"""The ten most played tracks of an artist, with 30 second previews.

Providers come from the settings (Deezer, then Last.fm by default). Deezer gives the list and the
preview addresses together; they are signed and expire after minutes, so the page asks for a new
run when they are old. Last.fm gives the list only: the previews are then looked up at iTunes,
track by track, and a track without a sure match simply has no preview.
"""

from app.integrations.breaker import ServiceBreakerOpen
from app.integrations.cached_http import NotConfigured, ServiceError
from app.integrations.discovery_types import TopTrackData
from app.integrations.lastfm import LastFmClient
from app.integrations.previews import DeezerClient, ItunesClient
from app.integrations.providers import provider_order
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError, RetryLater
from app.jobs.handlers.fetch_artist_profile import ensure_deezer_artist
from app.jobs.registry import job_handler
from app.models import TOP_DEEZER, TOP_ITUNES, TOP_LASTFM, Artist
from app.services.artist_profile import TOP_LIMIT, store_top_tracks
from app.services.library.grouping import name_key
from app.services.musicbrainz.matching import AUTO_TITLE_SIMILARITY, title_similarity


def top_dedupe_key(artist_id: int) -> str:
    return f"top_tracks:{artist_id}"


@job_handler("fetch_top_tracks", queue="discovery", max_attempts=3, backoff_base=60)
async def fetch_top_tracks(ctx: JobContext) -> None:
    artist_id = int(ctx.payload["artist_id"])
    force = bool(ctx.payload.get("force", False))
    found = await ctx.run_db(
        lambda db: (a.name, a.mbid) if (a := db.get(Artist, artist_id)) else None
    )
    if found is None:
        raise PermanentJobError(f"artist {artist_id} not found")
    name, mbid = found
    await ctx.log(f"Looking for the top tracks of {name}")

    answered = False
    paused: list[ServiceBreakerOpen] = []
    tracks: list[tuple[TopTrackData, str | None]] = []
    source = ""
    for provider in provider_order(ctx.settings, "toptracks"):
        try:
            if provider == TOP_DEEZER:
                listed = await _deezer(ctx, artist_id)
            else:
                listed = await _lastfm(ctx, name, mbid, force)
        except NotConfigured as exc:
            await ctx.log(f"{provider}: skipped ({exc})")
            continue
        except ServiceBreakerOpen as exc:
            paused.append(exc)
            await ctx.log(f"{provider} is paused after repeated failures", "warning")
            continue
        except ServiceError as exc:
            await ctx.log(f"{provider} failed: {exc}", "warning")
            continue
        answered = True
        if listed:
            tracks, source = listed, provider
            await ctx.log(f"{provider}: {len(listed)} track(s)")
            break
        await ctx.log(f"{provider}: no top tracks")

    if not answered:
        if paused:
            raise RetryLater("The top-track providers are paused", min(e.delay for e in paused))
        raise ServiceError("no provider answered; the job will be retried")

    if source == TOP_LASTFM:  # no previews from Last.fm: iTunes adds them
        tracks = await _previews_from_itunes(ctx, name, [t for t, _ in tracks])

    await ctx.run_db(_save(artist_id, source or TOP_DEEZER, tracks))
    with_preview = sum(1 for t, p in tracks if p)
    await ctx.log(f"{name}: {len(tracks)} track(s) saved, {with_preview} with a preview")
    await ctx.progress(100, "Done")


def _save(artist_id: int, source: str, tracks):
    def save(db):
        store_top_tracks(db, artist_id, source, tracks)
        db.commit()

    return save


async def _deezer(ctx: JobContext, artist_id: int) -> list[tuple[TopTrackData, str | None]]:
    deezer_id = await ensure_deezer_artist(ctx, artist_id, picture=False)
    if deezer_id is None:
        return []
    async with DeezerClient(ctx.settings, ctx.session_factory) as deezer:
        top = await deezer.artist_top(deezer_id, limit=TOP_LIMIT)
    return [(t, TOP_DEEZER if t.preview_url else None) for t in top]


async def _lastfm(
    ctx: JobContext, name: str, mbid: str | None, force: bool
) -> list[tuple[TopTrackData, str | None]]:
    async with LastFmClient(ctx.settings, ctx.session_factory) as lastfm:
        top = await lastfm.top_tracks(
            mbid=mbid, name=None if mbid else name, limit=TOP_LIMIT, force=force
        )
    return [(t, None) for t in top]


async def _previews_from_itunes(
    ctx: JobContext, artist: str, tracks: list[TopTrackData]
) -> list[tuple[TopTrackData, str | None]]:
    """Add a preview to each track that iTunes has for sure: same artist, near-identical title."""
    result: list[tuple[TopTrackData, str | None]] = []
    wanted = name_key(artist)
    try:
        async with ItunesClient(ctx.settings, ctx.session_factory) as itunes:
            for index, track in enumerate(tracks):
                await ctx.checkpoint()
                await ctx.progress(50 + 45 * index / max(len(tracks), 1), track.title)
                hit = next(
                    (
                        c
                        for c in await itunes.search_songs(artist, track.title)
                        if c.preview_url
                        and name_key(c.artist or "") == wanted
                        and title_similarity(c.title, track.title) >= AUTO_TITLE_SIMILARITY
                    ),
                    None,
                )
                if hit is None:
                    result.append((track, None))
                    continue
                joined = TopTrackData(
                    track.title,
                    track.album or hit.album,
                    hit.duration_s,
                    hit.track_url or track.track_url,
                    hit.preview_url,
                )
                result.append((joined, TOP_ITUNES))
    except (ServiceError, ServiceBreakerOpen) as exc:
        await ctx.log(f"iTunes not available for the previews ({exc})", "warning")
        result += [(t, None) for t in tracks[len(result) :]]
    return result
