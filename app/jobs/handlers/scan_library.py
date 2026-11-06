"""Scan the music library (filesystem or Plex) and update artists, albums and tracks."""

import asyncio

from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.services.library import build_provider
from app.services.library.provider import LibraryError, ScanReporter
from app.services.library.scan import run_scan


@job_handler("scan_library", queue="library", max_attempts=2, backoff_base=60)
async def scan_library(ctx: JobContext) -> None:
    force = bool(ctx.payload.get("force", False))
    try:
        provider = build_provider(ctx.settings)
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
    except LibraryError as exc:  # configuration/mount problems: retrying will not help
        raise PermanentJobError(str(exc)) from exc
    await ctx.log(
        f"Scan finished in {summary.duration_s}s: {summary.files_seen} file(s), "
        f"{summary.files_read} read, {summary.files_skipped} unchanged, "
        f"{summary.errors} error(s); {summary.artists} artist(s), {summary.albums} album(s) "
        f"in this scan, {summary.tracks} track(s) total; removed {summary.tracks_removed} track(s)"
    )
