"""Daily cleanup: delete the finished jobs (and their logs) that are older than the retention."""

import asyncio

from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.services import audit, notifications
from app.services.discovery import EXTERNAL_ARTIST_DAYS, purge_external_artists

COMPACT_AFTER = 1000  # give disk space back once at least this many jobs were removed


def retention_policy(settings) -> engine.RetentionPolicy:
    return engine.RetentionPolicy(
        routine_days=settings.job_retention_routine_days,
        success_days=settings.job_retention_success_days,
        failed_days=settings.job_retention_failed_days,
        overrides=settings.retention_overrides,
    )


@job_handler("housekeeping", queue="default", max_attempts=1)
async def housekeeping(ctx: JobContext) -> None:
    policy = retention_policy(ctx.settings)
    removed = await ctx.run_db(lambda db: engine.purge_jobs(db, policy))
    extra = f"; per type: {policy.overrides}" if policy.overrides else ""
    await ctx.log(
        f"Removed {removed} finished job(s) and their logs. Kept for: routine jobs "
        f"{policy.routine_days} day(s), other successful ones {policy.success_days}, "
        f"failed ones {policy.failed_days}{extra}"
    )
    if removed >= COMPACT_AFTER:
        try:
            await asyncio.to_thread(engine.compact_database, ctx.session_factory)
            await ctx.log("The database file was compacted")
        except Exception as exc:  # noqa: BLE001 - a failed compaction must not fail the cleanup
            await ctx.log(f"Could not compact the database: {type(exc).__name__}", "warning")

    def tidy(db):
        old_entries = audit.purge(db, ctx.settings.audit_retention_days)
        old_notes = notifications.purge(db)
        db.commit()
        return old_entries, old_notes

    entries, notes = await ctx.run_db(tidy)
    await ctx.log(
        f"Removed {entries} audit entr(ies) older than {ctx.settings.audit_retention_days} day(s) "
        f"and {notes} read notification(s)"
    )
    forgotten = await ctx.run_db(lambda db: purge_external_artists(db, EXTERNAL_ARTIST_DAYS))
    await ctx.log(f"Removed {forgotten} artist(s) opened from a search and never added")
    await ctx.progress(100, "done")
