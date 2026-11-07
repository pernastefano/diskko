"""Update what is known about one album: its release group at MusicBrainz, its track list (with
the artist of each track), its previews and its cover."""

from sqlalchemy import select

from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzNotConfigured,
)
from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.handlers.fetch_tracklist import tracklist_dedupe_key
from app.jobs.handlers.resolve_previews import previews_dedupe_key
from app.jobs.registry import job_handler
from app.models import SOURCE_MUSICBRAINZ, ReleaseGroup
from app.services.musicbrainz import matching, sync


def refresh_dedupe_key(release_group_id: int) -> str:
    return f"refresh_album:{release_group_id}"


@job_handler("refresh_album", queue="musicbrainz", max_attempts=3, backoff_base=60)
async def refresh_album(ctx: JobContext) -> None:
    group_id = int(ctx.payload["release_group_id"])
    found = await ctx.run_db(
        lambda db: (
            (g.mbid, g.title, g.artist_id, g.source)
            if (g := db.get(ReleaseGroup, group_id))
            else None
        )
    )
    if found is None:
        raise PermanentJobError(f"release group {group_id} not found")
    mbid, title, artist_id, source = found
    on_musicbrainz = source == SOURCE_MUSICBRAINZ
    await ctx.log(f"Updating {title}")

    if on_musicbrainz:
        try:
            client = MusicBrainzClient(ctx.settings, ctx.session_factory)
        except MusicBrainzNotConfigured as exc:
            raise PermanentJobError(str(exc)) from exc
        async with client:
            try:
                data = await client.lookup_release_group(mbid, force=True)
            except MusicBrainzError:
                raise  # the job engine retries with backoff
        if data is None:
            await ctx.log(
                f"{title}: MusicBrainz does not know this release group any more", "warning"
            )
        else:

            def save(db):
                sync.store_release_groups(db, artist_id, [data], prune=False)
                matching.match_artist_albums(db, artist_id)
                db.commit()

            await ctx.run_db(save)
            await ctx.log(f"{title}: details updated ({data.category}, {data.year or 'no year'})")
    await ctx.progress(50, "Queueing the rest")

    def queue(db):
        def child(type_, payload, key):
            engine.enqueue(db, type_, payload, dedupe_key=key, parent_job_id=ctx.job_id)

        if on_musicbrainz:
            child(
                "fetch_tracklist",
                {"release_group_id": group_id, "force": True},
                tracklist_dedupe_key(group_id),
            )
        child(
            "resolve_previews",
            {"release_group_id": group_id, "force": True},
            previews_dedupe_key(group_id),
        )
        group = db.scalar(select(ReleaseGroup).where(ReleaseGroup.id == group_id))
        if ctx.settings.coverart_enabled and group is not None and group.cover_status != "ok":
            group.cover_status = "unknown"  # look for the cover again
            child("fetch_covers", {"artist_id": artist_id}, f"fetch_covers:{artist_id}")

    await ctx.run_db(queue)
    await ctx.log("Track list, previews and cover queued")
    await ctx.progress(100, "Done")
