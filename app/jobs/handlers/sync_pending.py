"""Queue a MusicBrainz sync for every artist that needs one (new, or metadata too old)."""

from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.handlers.sync_artist import sync_dedupe_key
from app.jobs.registry import job_handler
from app.services.musicbrainz import sync


@job_handler("sync_pending_artists", queue="default", max_attempts=1)
async def sync_pending_artists(ctx: JobContext) -> None:
    if not ctx.settings.mb_user_agent.strip():
        await ctx.log(
            "MB_USER_AGENT is not set: MusicBrainz sync skipped (see .env.example)", "warning"
        )
        return

    def queue(db) -> int:
        count = 0
        for artist_id in sync.artists_needing_sync(db, ctx.settings.mb_refresh_days):
            engine.enqueue(
                db,
                "sync_artist_metadata",
                {"artist_id": artist_id},
                dedupe_key=sync_dedupe_key(artist_id),
                parent_job_id=ctx.job_id,
            )
            count += 1
        return count

    queued = await ctx.run_db(queue)
    await ctx.log(f"Queued {queued} artist(s) for a MusicBrainz sync")
    await ctx.progress(100, "Done")
