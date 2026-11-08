"""Scan the music library (filesystem or Plex) and update artists, albums and tracks."""

import asyncio

from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.handlers.sync_artist import sync_dedupe_key
from app.jobs.registry import job_handler
from app.services.library import build_provider
from app.services.library.provider import LibraryError, LibraryUnavailable, ScanReporter
from app.services.library.scan import run_scan
from app.services.quality_profiles import load_profiles


@job_handler("scan_library", queue="library", max_attempts=2, backoff_base=60)
async def scan_library(ctx: JobContext) -> None:
    force = bool(ctx.payload.get("force", False))
    try:
        profiles = await ctx.run_db(load_profiles)
        provider = build_provider(ctx.settings, profiles)
        await ctx.log(
            f"Scanning the {provider.source} library" + (" (full rescan)" if force else "")
        )
        reporter = ScanReporter(ctx.session_factory, ctx.job_id)
        summary = await asyncio.to_thread(
            run_scan,
            ctx.session_factory,
            provider,
            reporter,
            force=force,
            max_removal_pct=ctx.settings.library_max_removal_pct,
        )
    except LibraryUnavailable:
        raise  # Plex is down for now: the engine retries with backoff
    except LibraryError as exc:  # configuration/mount problems: retrying will not help
        raise PermanentJobError(str(exc)) from exc
    if ctx.settings.mb_user_agent.strip():  # new artists get their MusicBrainz identity
        await ctx.run_db(
            lambda db: engine.enqueue(
                db, "sync_pending_artists", {}, dedupe_key="sync_pending_artists"
            )
        )
        if summary.resync_artist_ids:  # a new album their stored discography lacks: sync again

            def resync(db) -> None:
                for artist_id in summary.resync_artist_ids:
                    engine.enqueue(
                        db,
                        "sync_artist_metadata",
                        {"artist_id": artist_id},
                        dedupe_key=sync_dedupe_key(artist_id),
                        parent_job_id=ctx.job_id,
                    )

            await ctx.run_db(resync)
            await ctx.log(
                f"{len(summary.resync_artist_ids)} artist(s) have a new album without a "
                "MusicBrainz match: their discography is synced again"
            )
    await ctx.run_db(  # new artists get their picture
        lambda db: engine.enqueue(db, "fetch_artist_images", {}, dedupe_key="fetch_artist_images")
    )
    await ctx.log(
        f"Scan finished in {summary.duration_s}s: {summary.files_seen} file(s), "
        f"{summary.files_read} read, {summary.files_skipped} unchanged, "
        f"{summary.errors} error(s); {summary.artists} artist(s), {summary.albums} album(s) "
        f"in this scan, {summary.tracks} track(s) total; removed {summary.tracks_removed} track(s)"
        + (
            f" and {summary.other_source_albums_removed} album(s) of another library source"
            if summary.other_source_albums_removed
            else ""
        )
    )
