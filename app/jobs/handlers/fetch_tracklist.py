"""Fetch the track list of an album (release group) from MusicBrainz."""

from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzNotConfigured,
)
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.models import Artist, ReleaseGroup
from app.services.tracklists import choose_release, store_tracklist


def tracklist_dedupe_key(release_group_id: int) -> str:
    return f"tracklist:{release_group_id}"


@job_handler("fetch_tracklist", queue="musicbrainz", max_attempts=3, backoff_base=60)
async def fetch_tracklist(ctx: JobContext) -> None:
    group_id = int(ctx.payload["release_group_id"])
    force = bool(ctx.payload.get("force", False))
    found = await ctx.run_db(
        lambda db: (
            (g.mbid, g.title, db.get(Artist, g.artist_id).name)
            if (g := db.get(ReleaseGroup, group_id))
            else None
        )
    )
    if found is None:
        raise PermanentJobError(f"release group {group_id} not found")
    mbid, title, artist = found
    if ":" in mbid:  # an album known only to a fallback provider has no MusicBrainz tracklist
        raise PermanentJobError(f"{title} is not on MusicBrainz: no tracklist to fetch")
    try:
        client = MusicBrainzClient(ctx.settings, ctx.session_factory)
    except MusicBrainzNotConfigured as exc:
        raise PermanentJobError(str(exc)) from exc
    await ctx.log(f"Fetching the tracks of {artist} – {title} (release group {mbid})")
    async with client:
        try:
            releases = await client.browse_releases(mbid, force=force)
        except MusicBrainzError:
            raise  # the job engine retries with backoff
    release = choose_release(releases or [])

    def save(db):
        count = store_tracklist(db, group_id, release)
        db.commit()
        return count

    count = await ctx.run_db(save)
    if release:
        await ctx.log(
            f"{count} track(s) from release {release.mbid} "
            f"({release.date or 'no date'}, {len(releases or [])} release(s) seen)"
        )
    else:
        await ctx.log("MusicBrainz lists no tracks for this album")
    await ctx.progress(100, "Done")
