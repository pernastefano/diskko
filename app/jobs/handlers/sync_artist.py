"""MusicBrainz sync: resolve the artist, store the discography, match the albums."""

from app.integrations.breaker import ServiceBreakerOpen
from app.integrations.cached_http import ServiceError
from app.integrations.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzNotConfigured,
)
from app.integrations.previews import DeezerClient
from app.integrations.providers import metadata_fallback_enabled
from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.models import MB_IGNORED, MB_PROVIDER, MB_RESOLVED, MB_REVIEW, MB_UNMATCHED, SOURCE_DEEZER
from app.services import metadata_fallback
from app.services.musicbrainz import matching, resolution, sync


def sync_dedupe_key(artist_id: int) -> str:
    return f"sync_artist:{artist_id}"


@job_handler("sync_artist_metadata", queue="musicbrainz", max_attempts=3, backoff_base=60)
async def sync_artist_metadata(ctx: JobContext) -> None:
    artist_id = int(ctx.payload["artist_id"])
    force = bool(ctx.payload.get("force", False))
    data = await ctx.run_db(lambda db: sync.load_artist_input(db, artist_id))
    if data is None:
        raise PermanentJobError(f"artist {artist_id} not found")
    await ctx.log(
        f"Syncing {data.name}: MusicBrainz status {data.mb_status}, "
        f"MBID {data.mbid or 'not known yet'}" + (" (forced)" if force else "")
    )
    if data.mb_status in (MB_UNMATCHED, MB_IGNORED) and not force:
        await ctx.log(f"{data.name}: skipped ({data.mb_status})")
        return
    if data.mb_status == MB_REVIEW and not force:
        await ctx.log(f"{data.name}: waiting for a manual decision in the review queue")
        return

    try:
        client = MusicBrainzClient(ctx.settings, ctx.session_factory)
    except MusicBrainzNotConfigured as exc:
        raise PermanentJobError(str(exc)) from exc

    fallback = metadata_fallback_enabled(ctx.settings)
    async with client:
        mbid = data.mbid
        if not mbid:
            await ctx.log(f"Looking up {data.name!r} on MusicBrainz")
            await ctx.progress(5, "Searching the artist")
            result = await resolution.resolve_artist(client, data.name, data.album_titles)
            if result.status != MB_RESOLVED:
                # Not on MusicBrainz (no artist at all, or it was only there for Deezer data):
                # a very sure Deezer match fills in; otherwise nothing changes.
                from_provider = data.mb_status == MB_PROVIDER
                asks_deezer = fallback and (
                    from_provider or (result.status == MB_REVIEW and result.reason == "no_match")
                )
                if asks_deezer and await _sync_from_deezer(ctx, data, force):
                    return
                if from_provider:  # keep what we have, and keep looking on later syncs
                    await ctx.log(f"{data.name}: still not on MusicBrainz, keeping Deezer data")
                    return
            await ctx.run_db(_commit(lambda db: sync.apply_resolution(db, artist_id, result)))
            if result.status != MB_RESOLVED:
                await ctx.log(
                    f"{data.name}: {result.status} ({result.reason or 'no search needed'})"
                )
                await ctx.progress(100, "Needs review" if result.status == MB_REVIEW else "Done")
                return
            mbid = result.mbid
            # apply_resolution may have refused the MBID (already used by another artist)
            refreshed = await ctx.run_db(lambda db: sync.load_artist_input(db, artist_id))
            if refreshed.mb_status != MB_RESOLVED:
                await ctx.log(f"{data.name}: the MBID is used by another artist, needs review")
                return
        await ctx.checkpoint(force=True)

        async def page(done: int, total: int) -> None:
            await ctx.progress(10 + 60 * done / max(total, 1), f"Release groups {done}/{total}")

        if mbid == resolution.VARIOUS_ARTISTS_MBID:
            # Its "discography" is every compilation in the database: not worth browsing, and
            # compilations cannot be matched through an artist anyway.
            await ctx.log("Various Artists: discography not synced (compilations are not matched)")
            groups = []
        else:
            try:
                groups = await client.browse_release_groups(mbid, force=force, on_page=page)
            except MusicBrainzNotConfigured as exc:  # pragma: no cover - checked above
                raise PermanentJobError(str(exc)) from exc
            except MusicBrainzError:
                raise  # the job engine retries with backoff

    if groups is None:
        await ctx.log(f"{data.name}: MusicBrainz does not know the artist {mbid}", "warning")
        await ctx.run_db(_commit(lambda db: sync.mark_invalid_mbid(db, artist_id)))
        return

    await ctx.checkpoint(force=True)
    await ctx.progress(75, "Saving release groups")

    def save(db):
        added, updated, removed = sync.store_release_groups(db, artist_id, groups)
        stats = matching.match_artist_albums(db, artist_id)
        sync.finish_sync(db, artist_id)
        db.commit()
        return added, updated, removed, stats

    added, updated, removed, stats = await ctx.run_db(save)
    await ctx.log(
        f"{data.name}: {len(groups)} release group(s) ({added} new, {removed} removed); albums: "
        f"{stats.matched} matched, {stats.tentative} tentative, {stats.unmatched} unmatched, "
        f"{stats.manual} manual"
    )
    if fallback and stats.unmatched and mbid != resolution.VARIOUS_ARTISTS_MBID:
        await _fill_albums_from_deezer(ctx, data)
    if ctx.settings.coverart_enabled and groups:
        await ctx.run_db(
            lambda db: engine.enqueue(
                db,
                "fetch_covers",
                {"artist_id": artist_id},
                dedupe_key=f"fetch_covers:{artist_id}",
                parent_job_id=ctx.job_id,
            )
        )
    await ctx.progress(100, "Done")


async def _sync_from_deezer(ctx: JobContext, data, force: bool) -> bool:
    """The artist is not on MusicBrainz: take its discography from Deezer when it is certain
    who the artist is. False when Deezer cannot say (the artist then goes to review as before)."""
    async with DeezerClient(ctx.settings, ctx.session_factory) as deezer:
        if data.provider_artist_id:
            provider_id = data.provider_artist_id
            groups = await deezer.artist_albums(provider_id, force=force)
            details, confidence = None, None
        else:
            match = await metadata_fallback.find_artist(
                deezer, data.name, data.album_titles, force=force
            )
            if match is None:
                await ctx.log(f"{data.name}: not on MusicBrainz and no sure match at Deezer")
                return False
            provider_id, groups = match.provider_id, match.groups
            confidence = min(0.5 + 0.1 * match.overlap, 0.95)
            details = {
                "provider_id": provider_id,
                "name": match.name,
                "fans": match.fans,
                "overlap": match.overlap,
                "url": f"https://www.deezer.com/artist/{provider_id}",
            }
    await ctx.checkpoint(force=True)

    def save(db):
        if details is not None:
            sync.apply_provider(db, data.id, SOURCE_DEEZER, provider_id, confidence, details)
        added, _, removed = sync.store_release_groups(db, data.id, groups, source=SOURCE_DEEZER)
        stats = matching.match_artist_albums(db, data.id)
        sync.finish_provider_sync(db, data.id)
        db.commit()
        return added, removed, stats

    added, removed, stats = await ctx.run_db(save)
    await ctx.log(
        f"{data.name}: not on MusicBrainz, using Deezer: {len(groups)} album(s) "
        f"({added} new, {removed} removed); library albums: {stats.matched} matched, "
        f"{stats.tentative} tentative, {stats.unmatched} unmatched"
    )
    if ctx.settings.coverart_enabled and groups:
        await ctx.run_db(
            lambda db: engine.enqueue(
                db,
                "fetch_covers",
                {"artist_id": data.id},
                dedupe_key=f"fetch_covers:{data.id}",
                parent_job_id=ctx.job_id,
            )
        )
    await ctx.progress(100, "Done")
    return True


async def _fill_albums_from_deezer(ctx: JobContext, data) -> None:
    """The artist is on MusicBrainz but some library albums are not: look for them at Deezer.
    Best effort: a Deezer failure never fails the MusicBrainz sync."""
    titles = await ctx.run_db(lambda db: sync.unmatched_titles(db, data.id))
    if not titles:
        return
    try:
        async with DeezerClient(ctx.settings, ctx.session_factory) as deezer:
            match = await metadata_fallback.find_artist(deezer, data.name, data.album_titles)
    except (ServiceError, ServiceBreakerOpen) as exc:
        await ctx.log(
            f"{data.name}: Deezer not available for the missing albums ({exc})", "warning"
        )
        return
    if match is None:
        await ctx.log(f"{data.name}: {len(titles)} album(s) not on MusicBrainz, none at Deezer")
        return
    groups = metadata_fallback.albums_to_add(titles, match.groups)
    if not groups:
        await ctx.log(f"{data.name}: {len(titles)} album(s) not on MusicBrainz, none at Deezer")
        return

    def save(db):
        sync.store_release_groups(db, data.id, groups, source=SOURCE_DEEZER, prune=False)
        return matching.match_artist_albums(db, data.id)

    stats = await ctx.run_db(_commit_result(save))
    await ctx.log(
        f"{data.name}: {len(groups)} album(s) not on MusicBrainz found at Deezer; "
        f"still unmatched: {stats.unmatched}"
    )


def _commit_result(func):
    def run(db):
        result = func(db)
        db.commit()
        return result

    return run


def _commit(func):
    def run(db):
        func(db)
        db.commit()

    return run
