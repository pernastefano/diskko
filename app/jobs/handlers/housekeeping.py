"""Daily cleanup: delete finished jobs (and their logs) older than the retention period."""

from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.services.discovery import EXTERNAL_ARTIST_DAYS, purge_external_artists


@job_handler("housekeeping", queue="default", max_attempts=1)
async def housekeeping(ctx: JobContext) -> None:
    days = ctx.settings.job_retention_days

    removed = await ctx.run_db(lambda db: engine.purge_old(db, days))
    await ctx.log(f"Removed {removed} finished job(s) older than {days} days")
    forgotten = await ctx.run_db(lambda db: purge_external_artists(db, EXTERNAL_ARTIST_DAYS))
    await ctx.log(f"Removed {forgotten} artist(s) opened from a search and never added")
    await ctx.progress(100, "done")
