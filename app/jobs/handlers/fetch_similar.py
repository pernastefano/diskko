"""Similar artists: Last.fm first (with an API key), ListenBrainz as the fallback."""

from app.integrations.cached_http import NotConfigured, ServiceError
from app.integrations.discovery_types import SimilarHit
from app.integrations.lastfm import LastFmClient
from app.integrations.listenbrainz import ListenBrainzClient
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.models import SIMILAR_LASTFM, SIMILAR_LISTENBRAINZ, Artist
from app.services.discovery import SIMILAR_MAX, store_similar


def similar_dedupe_key(artist_id: int) -> str:
    return f"fetch_similar:{artist_id}"


@job_handler("fetch_similar_artists", queue="discovery", max_attempts=3, backoff_base=60)
async def fetch_similar_artists(ctx: JobContext) -> None:
    artist_id = int(ctx.payload["artist_id"])
    force = bool(ctx.payload.get("force", False))
    found = await ctx.run_db(
        lambda db: (a.name, a.mbid) if (a := db.get(Artist, artist_id)) else None
    )
    if found is None:
        raise PermanentJobError(f"artist {artist_id} not found")
    name, mbid = found

    hits: list[SimilarHit] = []
    source: str | None = None
    answered = False  # did at least one provider give a definite answer (even "nothing")?
    tried = 0

    try:
        lastfm = LastFmClient(ctx.settings, ctx.session_factory)
    except NotConfigured:
        lastfm = None
        await ctx.log("Last.fm is not configured: skipping it")
    if lastfm is not None:
        tried += 1
        async with lastfm:
            try:
                hits = await lastfm.similar(
                    mbid=mbid, name=None if mbid else name, limit=SIMILAR_MAX, force=force
                )
                answered = True
                source = SIMILAR_LASTFM if hits else None
                await ctx.log(f"Last.fm: {len(hits)} similar artist(s) for {name}")
            except ServiceError as exc:
                await ctx.log(f"Last.fm failed: {exc}", "warning")
    await ctx.progress(50, "Last.fm done")

    if not hits and mbid:
        try:
            lb = ListenBrainzClient(ctx.settings, ctx.session_factory)
        except NotConfigured:
            lb = None
            await ctx.log("ListenBrainz needs MB_USER_AGENT (a contact): skipping it")
        if lb is not None:
            tried += 1
            async with lb:
                try:
                    hits = await lb.similar(mbid, limit=SIMILAR_MAX, force=force)
                    answered = True
                    source = SIMILAR_LISTENBRAINZ if hits else source
                    await ctx.log(f"ListenBrainz: {len(hits)} similar artist(s) for {name}")
                except ServiceError as exc:
                    await ctx.log(f"ListenBrainz failed: {exc}", "warning")

    if not tried:
        raise PermanentJobError("no similar-artists provider is configured")
    if not answered:
        raise ServiceError("no provider answered; the job will be retried")

    kept = await ctx.run_db(_save(artist_id, hits, source))
    await ctx.log(f"{name}: {kept} similar artist(s) saved" + (f" from {source}" if source else ""))
    await ctx.progress(100, "Done")


def _save(artist_id: int, hits: list[SimilarHit], source: str | None):
    def save(db):
        kept = store_similar(db, artist_id, hits, source)
        db.commit()
        return kept

    return save
