"""Nightly: queue a cover job for every artist that still has covers to fetch.

Opening an artist page already does it for that artist; this completes the artists nobody opens,
a batch a night so a big library is done over a few nights.
"""

from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.services.covers import artists_needing_covers
from app.services.musicbrainz.missing import get_missing_types

BATCH = 200  # artists per night


@job_handler("queue_pending_covers", queue="default", max_attempts=1)
async def queue_pending_covers(ctx: JobContext) -> None:
    if not ctx.settings.coverart_enabled:
        await ctx.log("Cover art is turned off: nothing to queue")
        return

    def queue(db) -> tuple[list[str], int]:
        todo = artists_needing_covers(db, get_missing_types(db), BATCH)
        for artist_id, _ in todo:
            engine.enqueue(
                db,
                "fetch_covers",
                {"artist_id": artist_id},
                dedupe_key=f"fetch_covers:{artist_id}",
                parent_job_id=ctx.job_id,
            )
        return [name for _, name in todo], len(todo)

    names, count = await ctx.run_db(queue)
    if not count:
        await ctx.log("Every artist has all the covers it can have")
    else:
        shown = ", ".join(names[:15]) + (f" and {count - 15} more" if count > 15 else "")
        await ctx.log(f"Queued the covers of {count} artist(s): {shown}")
        if count == BATCH:
            await ctx.log("There may be more: the next night continues")
    await ctx.progress(100, "Done")
