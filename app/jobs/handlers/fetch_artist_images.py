"""Find the picture of the library artists that have none (Deezer, one lookup per artist)."""

from app.integrations.cached_http import ServiceError
from app.integrations.previews import DeezerClient
from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.services.artist_images import artists_needing_image, pick_artist, store_image

BATCH = 150  # artists per run: a long library is done over a few runs, not in one long job
DEDUPE_KEY = "fetch_artist_images"


@job_handler("fetch_artist_images", queue="discovery", max_attempts=2, backoff_base=120)
async def fetch_artist_images(ctx: JobContext) -> None:
    todo = await ctx.run_db(lambda db: artists_needing_image(db, BATCH))
    if not todo:
        await ctx.log("Every artist already has a picture (or was looked up recently)")
        return
    await ctx.log(f"Looking up the picture of {len(todo)} artist(s)")
    found = missing = 0
    async with DeezerClient(ctx.settings, ctx.session_factory) as client:
        for done, (artist_id, name) in enumerate(todo):
            await ctx.checkpoint()
            try:
                candidates = await client.search_artists(name)
            except ServiceError as exc:
                await ctx.log(
                    f"{name}: Deezer failed ({exc}); stopping, the rest is for next time", "warning"
                )
                raise
            pick = pick_artist(candidates, name)

            def save(db, aid=artist_id, url=pick.picture_url if pick else None):
                store_image(db, aid, url)
                db.commit()

            await ctx.run_db(save)
            if pick:
                found += 1
                await ctx.log(f"{name}: picture found ({pick.fans} fans)")
            else:
                missing += 1
                await ctx.log(
                    f"{name}: no artist named exactly so at Deezer ({len(candidates)} result(s))"
                )
            await ctx.progress(100 * (done + 1) / len(todo), name)
    await ctx.log(f"Done: {found} picture(s) found, {missing} not found")
    if len(todo) == BATCH:  # there may be more: another run, queued behind this one
        await ctx.run_db(
            lambda db: (
                engine.enqueue(
                    db, "fetch_artist_images", {}, dedupe_key=DEDUPE_KEY, parent_job_id=ctx.job_id
                ),
                db.commit(),
            )
        )
