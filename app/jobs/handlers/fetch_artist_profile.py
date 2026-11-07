"""The artist's profile: the id and picture at Deezer, and a short biography per language.

Biography: Wikipedia first (found through MusicBrainz -> Wikidata, so it needs the artist's MBID
and the MusicBrainz contact), Last.fm as the fallback, in the order set in the settings. A
language that no provider has stays empty and is looked up again after a month.
"""

from app.integrations.breaker import ServiceBreakerOpen
from app.integrations.cached_http import NotConfigured, ServiceError
from app.integrations.discovery_types import BioData
from app.integrations.lastfm import LastFmClient
from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzNotConfigured,
)
from app.integrations.previews import DeezerClient
from app.integrations.providers import provider_order
from app.integrations.wikipedia import WikidataClient, WikipediaClient
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError, RetryLater
from app.jobs.registry import job_handler
from app.models import BIO_WIKIPEDIA, Artist, utcnow
from app.services.artist_profile import BIO_LANGS, pick_deezer_artist, store_bio


def profile_dedupe_key(artist_id: int) -> str:
    return f"artist_profile:{artist_id}"


async def ensure_deezer_artist(
    ctx: JobContext, artist_id: int, *, picture: bool = True
) -> str | None:
    """The artist's id at Deezer, looked up by exact name when not known yet; the picture is
    looked up too (`picture`) when the artist has none. None when Deezer has nobody by that
    name."""
    found = await ctx.run_db(
        lambda db: (
            (
                a.name,
                a.deezer_artist_id or (a.provider_artist_id if a.provider == "deezer" else None),
                bool(a.image_url),
            )
            if (a := db.get(Artist, artist_id))
            else None
        )
    )
    if found is None:
        raise PermanentJobError(f"artist {artist_id} not found")
    name, known, has_picture = found
    has_picture = has_picture or not picture  # not asked for: leave it alone
    async with DeezerClient(ctx.settings, ctx.session_factory) as deezer:
        if known:
            picture = None if has_picture else await deezer.artist_picture(known)
            deezer_id = known
        else:
            pick = pick_deezer_artist(await deezer.search_artists(name), name)
            if pick is None:
                await ctx.log(f"{name}: no artist named exactly so at Deezer")
                return None
            deezer_id, picture = pick.provider_id, None if has_picture else pick.picture_url

    def save(db):
        artist = db.get(Artist, artist_id)
        artist.deezer_artist_id = deezer_id
        if picture and not artist.image_url:
            artist.image_url = picture
        if not has_picture:
            artist.image_checked_at = utcnow()
        db.commit()

    await ctx.run_db(save)
    if not known:
        await ctx.log(f"{name}: Deezer artist {deezer_id}")
    if not has_picture:
        await ctx.log(f"{name}: picture {'found' if picture else 'not found at Deezer'}")
    return deezer_id


@job_handler("fetch_artist_profile", queue="discovery", max_attempts=3, backoff_base=60)
async def fetch_artist_profile(ctx: JobContext) -> None:
    artist_id = int(ctx.payload["artist_id"])
    force = bool(ctx.payload.get("force", False))
    found = await ctx.run_db(
        lambda db: (a.name, a.mbid) if (a := db.get(Artist, artist_id)) else None
    )
    if found is None:
        raise PermanentJobError(f"artist {artist_id} not found")
    name, mbid = found
    await ctx.log(f"Looking up the profile of {name}")

    answered = False  # did a provider (or Deezer) give a definite answer?
    paused: list[ServiceBreakerOpen] = []

    try:  # the picture and the id at Deezer
        await ensure_deezer_artist(ctx, artist_id)
        answered = True
    except ServiceBreakerOpen as exc:
        paused.append(exc)
    except ServiceError as exc:
        await ctx.log(f"Deezer failed: {exc}", "warning")
    await ctx.progress(30, "Picture")

    bios: dict[str, tuple[BioData, str]] = {}
    for provider in provider_order(ctx.settings, "bio"):
        missing = [lang for lang in BIO_LANGS if lang not in bios]
        if not missing:
            break
        try:
            if provider == BIO_WIKIPEDIA:
                got = await _wikipedia(ctx, mbid, tuple(missing), force)
            else:
                got = await _lastfm(ctx, name, mbid, tuple(missing), force)
        except NotConfigured as exc:
            await ctx.log(f"{provider}: skipped ({exc})")
            continue
        except ServiceBreakerOpen as exc:
            paused.append(exc)
            await ctx.log(f"{provider} is paused after repeated failures", "warning")
            continue
        except (ServiceError, MusicBrainzError) as exc:
            await ctx.log(f"{provider} failed: {exc}", "warning")
            continue
        if got is None:  # this provider cannot say anything for this artist (no MBID)
            continue
        answered = True
        for lang, bio in got.items():
            bios[lang] = (bio, provider)
        await ctx.log(f"{provider}: biography in {', '.join(got) or 'no language'}")
    await ctx.progress(80, "Biography")

    if not answered:
        if paused:
            raise RetryLater("The profile providers are paused", min(e.delay for e in paused))
        raise ServiceError("no provider answered; the job will be retried")

    def save(db):
        for lang in BIO_LANGS:
            bio, source = bios.get(lang, (None, None))
            store_bio(db, artist_id, lang, bio, source)
        db.get(Artist, artist_id).profile_checked_at = utcnow()
        db.commit()

    await ctx.run_db(save)
    await ctx.log(f"{name}: {len(bios)} biography(ies) saved")
    await ctx.progress(100, "Done")


async def _wikipedia(
    ctx: JobContext, mbid: str | None, langs: tuple[str, ...], force: bool
) -> dict[str, BioData] | None:
    """{language: biography}; None when Wikipedia cannot be asked (no MBID or no contact)."""
    if not mbid:
        return None
    try:
        mb = MusicBrainzClient(ctx.settings, ctx.session_factory)
    except MusicBrainzNotConfigured:
        return None
    async with mb:
        qid = await mb.artist_wikidata(mbid, force=force)
    if not qid:
        return {}
    async with WikidataClient(ctx.settings, ctx.session_factory) as wikidata:
        titles = await wikidata.sitelinks(qid, langs, force=force)
    result: dict[str, BioData] = {}
    async with WikipediaClient(ctx.settings, ctx.session_factory) as wikipedia:
        for lang, title in titles.items():
            bio = await wikipedia.summary(lang, title, force=force)
            if bio is not None:
                result[lang] = bio
    return result


async def _lastfm(
    ctx: JobContext, name: str, mbid: str | None, langs: tuple[str, ...], force: bool
) -> dict[str, BioData]:
    result: dict[str, BioData] = {}
    async with LastFmClient(ctx.settings, ctx.session_factory) as lastfm:
        for lang in langs:
            bio = await lastfm.bio(mbid=mbid, name=None if mbid else name, lang=lang, force=force)
            if bio is not None:
                result[lang] = bio
    return result
