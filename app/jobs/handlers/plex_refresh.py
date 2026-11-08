"""Scan the folders that were imported in Plex, wait for it, check the albums are there and
reconcile the Plex rating keys.

Partial scans of the album folders first; the whole section when Plex refuses one, when an album
does not show up after them (Plex ignores a path it does not know without saying so) or when
there are many folders. The import that asked for this never fails because of Plex: this job is
retried on its own and the folders stay in the list until a scan went through.
"""

import asyncio

from plexapi.exceptions import PlexApiException, Unauthorized

from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError, RetryLater
from app.jobs.registry import job_handler
from app.services import plex_refresh as svc
from app.services.library.provider import LibraryError

START_GRACE = 5.0  # seconds Plex may take to flag the section as being scanned
POLL = 3.0  # seconds between two looks at the flag


def _scanner(ctx: JobContext) -> svc.PlexScanner:
    try:
        return svc.PlexScanner(ctx.settings)
    except LibraryError as exc:
        message = str(exc)
        if "Cannot connect" in message or "Plex error" in message:
            raise  # the connection: worth another try
        raise PermanentJobError(message) from exc


async def _wait_for_plex(ctx: JobContext, scanner: svc.PlexScanner) -> bool:
    """True when Plex finished scanning, False when `plex_refresh_timeout_minutes` ran out."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    limit = ctx.settings.plex_refresh_timeout_minutes * 60
    seen = False
    while True:
        await ctx.checkpoint()
        busy = await asyncio.to_thread(scanner.is_refreshing)
        seen = seen or busy
        elapsed = loop.time() - started
        if not busy and (seen or elapsed >= START_GRACE):
            return True
        if elapsed >= limit:
            return False
        await asyncio.sleep(POLL)


async def _scan_and_wait(ctx, scanner, *, full: bool, paths: list[str] | None = None) -> str:
    """Run the scans. Returns "done", "timeout" or "refused" (a partial scan was refused)."""
    if full:
        await asyncio.to_thread(scanner.scan_all)
        await ctx.log("Scanning the whole Plex section")
    else:
        for path in paths or []:
            try:
                sent = await asyncio.to_thread(scanner.scan_path, path)
            except svc.PlexRefused as exc:
                await ctx.log(str(exc), "warning")
                return "refused"
            await ctx.log(f"Scanning {sent} in Plex")
    return "done" if await _wait_for_plex(ctx, scanner) else "timeout"


async def _find_all(scanner: svc.PlexScanner, items: list[svc.Pending]):
    """{folder: Plex album or None} for the imported folders."""
    found = {}
    for item in items:
        found[item.path] = await asyncio.to_thread(scanner.find_album, item)
    return found


async def _record_in_library(ctx: JobContext, present: dict) -> int:
    """Add the albums Plex now has to DISKKO's library (artist, album, tracks, rating keys) and
    match them with MusicBrainz. Returns how many were recorded; never raises."""
    from app.services.library.plex_provider import PlexProvider
    from app.services.library.scan import match_new_albums, save_artists
    from app.services.quality_profiles import load_profiles

    settings = ctx.settings
    try:
        profiles = await ctx.run_db(load_profiles)
        provider = PlexProvider(settings, profiles)
        built = []
        for album in present.values():
            artist = await asyncio.to_thread(provider.scanned_artist_of, album)
            if artist is not None:
                built.append(artist)
        if not built:
            return 0

        def save(db):
            ids = save_artists(db, provider.source, built)
            resync = match_new_albums(db, ids)
            db.commit()
            return ids, resync

        ids, resync = await ctx.run_db(save)
        if settings.mb_user_agent.strip():
            from app.jobs.handlers.sync_artist import sync_dedupe_key

            def queue(db):
                engine.enqueue(db, "sync_pending_artists", {}, dedupe_key="sync_pending_artists")
                for artist_id in resync:
                    engine.enqueue(
                        db,
                        "sync_artist_metadata",
                        {"artist_id": artist_id},
                        dedupe_key=sync_dedupe_key(artist_id),
                        parent_job_id=ctx.job_id,
                    )

            await ctx.run_db(queue)
        titles = ", ".join(a.albums[0].title for a in built)
        await ctx.log(f"Added to the library: {titles}")
        return len(built)
    except Exception as exc:  # noqa: BLE001 - a scan of Plex is the fallback
        await ctx.log(f"Could not add the album(s) directly ({type(exc).__name__})", "warning")
        return 0


@job_handler("plex_refresh", queue="default", max_attempts=3, backoff_base=60)
async def plex_refresh(ctx: JobContext) -> None:
    settings = ctx.settings
    full_requested = bool(ctx.payload.get("full", False))
    if not svc.plex_configured(settings):
        await ctx.log("Plex is not configured: nothing to do")
        return
    pending = [] if full_requested else await ctx.run_db(svc.read_pending)
    if not pending and not full_requested:
        await ctx.log("Nothing waits for Plex")
        return

    try:
        scanner = await asyncio.to_thread(_scanner, ctx)
        if full_requested:
            await ctx.log("Scan of the whole section, as requested")
            outcome = await _scan_and_wait(ctx, scanner, full=True)
            found: dict = {}
        else:
            await ctx.log(f"{len(pending)} imported folder(s) to show Plex")
            full = len(pending) > svc.FULL_SCAN_PATHS
            if full:
                await ctx.log(f"More than {svc.FULL_SCAN_PATHS} folders: scanning the section")
            outcome = await _scan_and_wait(ctx, scanner, full=full, paths=[p.path for p in pending])
            if outcome == "refused":  # a partial scan was not accepted
                full = True
                outcome = await _scan_and_wait(ctx, scanner, full=True)
            found = await _find_all(scanner, pending) if outcome == "done" else {}
            missing = [p for p in pending if found.get(p.path) is None]
            if outcome == "done" and missing and not full:
                await ctx.log(
                    f"{len(missing)} album(s) not in Plex after the partial scan: "
                    "scanning the whole section",
                    "warning",
                )
                outcome = await _scan_and_wait(ctx, scanner, full=True)
                found = await _find_all(scanner, pending) if outcome == "done" else {}
    except (Unauthorized, PlexApiException) as exc:
        if isinstance(exc, Unauthorized):
            raise PermanentJobError("Plex rejected the token (unauthorized)") from exc
        raise

    if outcome == "timeout":
        await ctx.log(
            f"Plex is still scanning after {settings.plex_refresh_timeout_minutes} minute(s): "
            "the albums will show up when it is done",
            "warning",
        )
    present = {path: album for path, album in found.items() if album is not None}
    for item in pending:
        if outcome == "done" and found.get(item.path) is None:
            await ctx.log(
                f"{item.title or item.path}: not found in Plex after a full scan", "warning"
            )
        elif item.path in present:
            await ctx.log(
                f"{item.title or item.path}: in Plex (rating key {present[item.path].ratingKey})"
            )

    if present and settings.plex_refresh_metadata:
        for album in present.values():
            await asyncio.to_thread(scanner.refresh_metadata, album)
        await ctx.log(f"Plex was asked to refresh the metadata of {len(present)} album(s)")

    if settings.library_source.strip().lower() == "plex":
        # The albums are in Plex now: put just them in the library (no scan of everything), with
        # their rating keys. If that cannot be done, the next scan of Plex will.
        recorded = await _record_in_library(ctx, present) if present else 0
        if present and recorded < len(present):
            await ctx.run_db(
                lambda db: engine.enqueue(
                    db, "scan_library", {}, dedupe_key="scan_library", parent_job_id=ctx.job_id
                )
            )
            await ctx.log("A library scan was queued to record what could not be added directly")
    elif present:

        def reconcile(db):
            totals = [0, 0]
            for album in present.values():
                a, t = svc.reconcile_keys(db, scanner, album, str(settings.music_dir))
                totals[0] += a
                totals[1] += t
            db.commit()
            return totals

        albums, tracks = await ctx.run_db(reconcile)
        await ctx.log(f"Plex rating keys recorded on {albums} album(s) and {tracks} track(s)")

    def finish(db):
        left = svc.remove_pending(db, pending) if pending else []
        svc.mark_finished(db)
        db.commit()
        return left

    left = await ctx.run_db(finish)
    if left:
        # Folders arrived while this job was running. A new job cannot be queued from here (this
        # one still holds the "plex_refresh" key, so the request would find it and stop), so this
        # one goes back in the queue and takes them: they are in the list.
        await ctx.log(f"{len(left)} more folder(s) arrived: another scan follows")
        raise RetryLater("more folders arrived", float(settings.plex_refresh_debounce_seconds))
    await ctx.progress(100, "Done")
