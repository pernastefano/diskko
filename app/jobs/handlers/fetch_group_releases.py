"""Read every release of a release group from MusicBrainz (to compare them with an album).

The answer lands in `api_cache`; the match page reads it from there.
"""

from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzNotConfigured,
)
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler


def group_releases_dedupe_key(mbid: str) -> str:
    return f"group_releases:{mbid}"


@job_handler("fetch_group_releases", queue="musicbrainz", max_attempts=2, backoff_base=30)
async def fetch_group_releases(ctx: JobContext) -> None:
    mbid = str(ctx.payload.get("release_group_mbid", "")).strip()
    if not mbid or ":" in mbid:  # an album of a fallback provider has no MusicBrainz releases
        raise PermanentJobError("not a MusicBrainz release group")
    try:
        client = MusicBrainzClient(ctx.settings, ctx.session_factory)
    except MusicBrainzNotConfigured as exc:
        raise PermanentJobError(str(exc)) from exc
    await ctx.log(f"Reading the releases of release group {mbid}")
    async with client:
        try:
            releases = await client.browse_releases(mbid, everything=True)
        except MusicBrainzError:
            raise  # the job engine retries with backoff
    await ctx.log(f"{len(releases or [])} release(s)")
    await ctx.progress(100, "Done")
