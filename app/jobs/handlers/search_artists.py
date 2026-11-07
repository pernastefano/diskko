"""Search MusicBrainz for an artist by name (also artists that are not in the library).

The answer lands in `api_cache`; the search page reads it from there.
"""

import hashlib

from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzNotConfigured,
)
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler

SEARCH_LIMIT = 15


def search_dedupe_key(query: str) -> str:
    return "search_artists:" + hashlib.sha1(query.encode()).hexdigest()[:16]


@job_handler("search_artists", queue="musicbrainz", max_attempts=2, backoff_base=30)
async def search_artists(ctx: JobContext) -> None:
    query = str(ctx.payload.get("query", "")).strip()
    if not query:
        raise PermanentJobError("empty search")
    try:
        client = MusicBrainzClient(ctx.settings, ctx.session_factory)
    except MusicBrainzNotConfigured as exc:
        raise PermanentJobError(str(exc)) from exc
    await ctx.log(f"Searching MusicBrainz for {query!r}")
    async with client:
        try:
            hits = await client.search_artists(query, limit=SEARCH_LIMIT)
        except MusicBrainzError:
            raise  # the job engine retries with backoff
    await ctx.log(f"{len(hits)} artist(s) found")
    await ctx.progress(100, "Done")
