"""Similar artists from the configured providers (Last.fm with an API key, then ListenBrainz
by default)."""

from app.integrations.breaker import ServiceBreakerOpen
from app.integrations.cached_http import NotConfigured, ServiceError
from app.integrations.discovery_types import SimilarHit
from app.integrations.lastfm import LastFmClient
from app.integrations.listenbrainz import ListenBrainzClient
from app.integrations.providers import provider_order
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError, RetryLater
from app.jobs.registry import job_handler
from app.models import SIMILAR_LASTFM, Artist
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
    paused: list[ServiceBreakerOpen] = []

    order = provider_order(ctx.settings, "similar")
    for position, provider in enumerate(order, start=1):
        if hits:
            break
        try:
            if provider == SIMILAR_LASTFM:
                client = LastFmClient(ctx.settings, ctx.session_factory)
                label = "Last.fm"
            else:
                if not mbid:
                    continue  # ListenBrainz works from the MusicBrainz id only
                client = ListenBrainzClient(ctx.settings, ctx.session_factory)
                label = "ListenBrainz"
        except NotConfigured:
            if provider == SIMILAR_LASTFM:
                await ctx.log("Last.fm is not configured: skipping it")
            else:
                await ctx.log("ListenBrainz needs MB_USER_AGENT (a contact): skipping it")
            continue
        tried += 1
        async with client:
            try:
                if provider == SIMILAR_LASTFM:
                    found = await client.similar(
                        mbid=mbid, name=None if mbid else name, limit=SIMILAR_MAX, force=force
                    )
                else:
                    found = await client.similar(mbid, limit=SIMILAR_MAX, force=force)
                answered = True
                if found:
                    hits, source = found, provider
                await ctx.log(f"{label}: {len(found)} similar artist(s) for {name}")
            except ServiceBreakerOpen as exc:
                paused.append(exc)
                await ctx.log(f"{label} is paused after repeated failures", "warning")
            except ServiceError as exc:
                await ctx.log(f"{label} failed: {exc}", "warning")
        await ctx.progress(100 * position / (len(order) + 1), f"{label} done")

    if not tried:
        raise PermanentJobError("no similar-artists provider is configured")
    if not answered:
        if paused:  # the service is paused: wait for it without using an attempt
            raise RetryLater("Similar-artists providers are paused", min(e.delay for e in paused))
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
