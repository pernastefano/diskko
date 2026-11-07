"""Recompute the quality of every track and album after the quality profiles changed."""

from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.services.quality_profiles import load_profiles, reclassify


def _run(db):
    changed = reclassify(db, load_profiles(db))
    db.commit()
    return changed


@job_handler("reclassify_quality", queue="library", max_attempts=2, backoff_base=30)
async def reclassify_quality(ctx: JobContext) -> None:
    await ctx.log("Recomputing the quality of the library")
    tracks, albums = await ctx.run_db(_run)
    await ctx.log(f"Done: {tracks} track(s) and {albums} album(s) changed")
