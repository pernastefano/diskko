"""Find 30 second previews for an album at the configured providers (iTunes, then Deezer by
default). Only addresses are kept."""

from app.integrations.breaker import ServiceBreakerOpen
from app.integrations.cached_http import ServiceError
from app.integrations.previews import DeezerClient, ItunesClient
from app.integrations.providers import provider_order
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError, RetryLater
from app.jobs.registry import job_handler
from app.models import PREVIEW_DEEZER, PREVIEW_ITUNES, Artist, ReleaseGroup
from app.services.previews import best_candidate, store_previews

CLIENTS = {PREVIEW_ITUNES: ItunesClient, PREVIEW_DEEZER: DeezerClient}


def previews_dedupe_key(release_group_id: int) -> str:
    return f"previews:{release_group_id}"


@job_handler("resolve_previews", queue="discovery", max_attempts=3, backoff_base=60)
async def resolve_previews(ctx: JobContext) -> None:
    rg_id = int(ctx.payload["release_group_id"])
    force = bool(ctx.payload.get("force", False))

    def load(db):
        rg = db.get(ReleaseGroup, rg_id)
        artist = db.get(Artist, rg.artist_id) if rg else None
        return (artist.name, rg.title, rg.track_count_hint) if rg and artist else None

    found = await ctx.run_db(load)
    if found is None:
        raise PermanentJobError(f"release group {rg_id} not found")
    artist, title, hint = found
    await ctx.log(f"Looking for previews of {artist} - {title}")

    answered = 0  # providers that gave a definite answer, even "no such album"
    paused: list[ServiceBreakerOpen] = []
    for provider in provider_order(ctx.settings, "previews"):
        factory = CLIENTS[provider]
        async with factory(ctx.settings, ctx.session_factory) as client:
            try:
                candidates = await client.search_albums(artist, title, force=force)
                pick = best_candidate(candidates, artist, title, hint)
                tracks = []
                if pick is not None:
                    tracks = (
                        await client.album_tracks(pick.provider_id, force=force)
                        if provider == PREVIEW_ITUNES
                        else await client.album_tracks(pick.provider_id)
                    )
                answered += 1
            except ServiceBreakerOpen as exc:
                paused.append(exc)
                await ctx.log(f"{provider} is paused after repeated failures", "warning")
                continue
            except ServiceError as exc:
                await ctx.log(f"{provider} failed: {exc}", "warning")
                continue
        if pick is None:
            await ctx.log(f"{provider}: no matching album ({len(candidates)} candidate(s))")
            continue
        if not tracks:
            await ctx.log(f"{provider}: album {pick.provider_id} has no previews")
            continue
        await ctx.run_db(_save(rg_id, provider, pick.provider_id, tracks, pick.url))
        await ctx.log(f"{provider}: {len(tracks)} preview(s) saved")
        await ctx.progress(100, "Done")
        return

    if not answered:
        if paused:  # nothing was wrong with this job: wait for the service, keep the attempt
            raise RetryLater("Preview providers are paused", min(e.delay for e in paused))
        raise ServiceError("no preview provider answered; the job will be retried")
    await ctx.run_db(_save(rg_id, None, None, []))
    await ctx.log("No previews available for this album")
    await ctx.progress(100, "Done")


def _save(rg_id, provider, album_id, tracks, url=None):
    def save(db):
        store_previews(db, rg_id, provider, album_id, tracks, url)
        db.commit()

    return save
