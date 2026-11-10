"""Look up, on MusicBrainz, how many tracks each album of the library should have.

The albums page can then list the albums that have fewer (or more) files than the release. One
request per album, at the pace MusicBrainz allows, in the background: on the `library` queue,
so searches and other quick jobs that use MusicBrainz are not kept waiting behind it.
"""

from sqlalchemy import or_, select

from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzNotConfigured,
)
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.models import LibraryAlbum, ReleaseGroup, TracklistFetch, utcnow
from app.services.musicbrainz.missing import OWNED_STATUSES
from app.services.tracklists import REFRESH, pick_release, store_tracklist

DEDUPE_KEY = "check_tracklists"
MAX_IN_A_ROW = 5  # failures in a row that mean MusicBrainz is not answering: stop and retry later


def groups_to_check(db) -> list[tuple[int, str, str]]:
    """(id, mbid, title) of the owned MusicBrainz albums with no recent track list."""
    stale = utcnow() - REFRESH
    rows = db.execute(
        select(ReleaseGroup.id, ReleaseGroup.mbid, ReleaseGroup.title)
        .join(LibraryAlbum, LibraryAlbum.release_group_id == ReleaseGroup.id)
        .outerjoin(TracklistFetch, TracklistFetch.release_group_id == ReleaseGroup.id)
        .where(
            LibraryAlbum.match_status.in_(OWNED_STATUSES),
            ReleaseGroup.source == "musicbrainz",
            or_(TracklistFetch.release_group_id.is_(None), TracklistFetch.fetched_at < stale),
        )
        .distinct()
        .order_by(ReleaseGroup.id)
    )
    return [(r[0], r[1], r[2]) for r in rows if ":" not in r[1]]


@job_handler("check_tracklists", queue="library", max_attempts=2, backoff_base=300)
async def check_tracklists(ctx: JobContext) -> None:
    todo = await ctx.run_db(groups_to_check)
    if not todo:
        await ctx.log("Every album already has its MusicBrainz track list")
        await ctx.progress(100, "Done")
        return
    try:
        client = MusicBrainzClient(ctx.settings, ctx.session_factory)
    except MusicBrainzNotConfigured as exc:
        raise PermanentJobError(str(exc)) from exc
    await ctx.log(f"{len(todo)} album(s) to look up (about {len(todo)} s)")
    in_a_row = 0
    async with client:
        for n, (group_id, mbid, title) in enumerate(todo, start=1):
            await ctx.checkpoint()
            await ctx.progress(100 * (n - 1) / len(todo), title)
            try:
                releases = await client.browse_releases(mbid)
            except MusicBrainzError as exc:
                in_a_row += 1
                await ctx.log(f"{title}: {exc}", "warning")
                if in_a_row >= MAX_IN_A_ROW:
                    raise  # the engine tries again later
                continue
            in_a_row = 0

            def save(db, group_id=group_id, releases=releases):
                store_tracklist(db, group_id, pick_release(db, group_id, releases or []))
                db.commit()

            await ctx.run_db(save)
    await ctx.log(f"{len(todo)} album(s) checked")
    await ctx.progress(100, "Done")
