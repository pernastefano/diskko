"""Search the Soulseek network (through SLSKD) for the album of a download request."""

import asyncio

from app.integrations.slskd_client import (
    SlskdAuthError,
    SlskdClient,
    SlskdError,
    SlskdNotConfigured,
)
from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.models import MODE_MANUAL, REQ_FAILED, REQ_QUEUED, REQ_SEARCHING, DownloadRequest
from app.services import downloads as svc
from app.services.candidate_selection import query_variants, select_candidates
from app.services.quality_profiles import load_profiles

MAX_ATTEMPTS = 2
PAUSE_BETWEEN_SEARCHES = 3.0  # the Soulseek server does not like a search storm


def _load(request_id: int):
    def load(db):
        request = db.get(DownloadRequest, request_id)
        if request is None:
            return None
        wanted, artist, album = svc.wanted_for(db, request)
        return {
            "status": request.status,
            "override": request.query_override,
            "mode": request.mode,
            "wanted": wanted,
            "artist": artist,
            "album": album,
        }

    return load


@job_handler("slskd_search", queue="slskd", max_attempts=MAX_ATTEMPTS, backoff_base=60)
async def slskd_search(ctx: JobContext) -> None:
    request_id = int(ctx.payload["request_id"])
    data = await ctx.run_db(_load(request_id))
    if data is None:
        raise PermanentJobError(f"download request {request_id} not found")
    if data["status"] not in (REQ_QUEUED, REQ_SEARCHING):
        await ctx.log(f"Skipped: the request is {data['status']}")
        return

    def fail(message: str):
        def save(db):
            svc.set_status(db, request_id, REQ_FAILED, error=message)
            db.commit()

        return save

    try:
        client = SlskdClient(ctx.settings)
    except SlskdNotConfigured as exc:
        await ctx.run_db(fail(str(exc)))
        raise PermanentJobError(str(exc)) from exc

    def mark_searching(db):
        svc.searching(db, request_id)
        db.commit()

    await ctx.run_db(mark_searching)
    wanted, artist, album = data["wanted"], data["artist"], data["album"]
    variants = [data["override"]] if data["override"] else query_variants(artist, album)
    profiles = await ctx.run_db(load_profiles)
    settings = ctx.settings
    timeout = settings.slskd_search_timeout

    candidates = []
    used = variants[-1]
    try:
        async with client:
            state = await client.server_state()
            if not (state["connected"] and state["logged_in"]):
                raise SlskdError("SLSKD is not connected to the Soulseek network")
            for i, query in enumerate(variants):
                await ctx.checkpoint(force=True)
                await ctx.progress(100 * i / len(variants), f"Searching: {query}")
                await ctx.log(f"Searching for {query!r}")
                search_id = await client.create_search(query, timeout_s=timeout)
                try:
                    info = await client.wait_for_search(search_id, hard_timeout=timeout * 3 + 20)
                    responses = await client.get_responses(search_id)
                finally:
                    await client.delete_search(search_id)
                await ctx.log(
                    f"{len(responses)} user(s) answered ({info.file_count} file(s), {info.state})"
                )
                candidates = select_candidates(
                    responses, wanted, profiles, limit=settings.slskd_candidates
                )
                used = query
                if candidates:
                    break
                await ctx.log("No suitable folder in the answers")
                if i < len(variants) - 1:
                    await asyncio.sleep(PAUSE_BETWEEN_SEARCHES)
    except SlskdAuthError as exc:
        await ctx.run_db(fail(str(exc)))
        raise PermanentJobError(str(exc)) from exc
    except SlskdError as exc:
        if ctx.attempt >= MAX_ATTEMPTS:
            await ctx.run_db(fail(f"SLSKD: {exc}"))
        raise

    if not candidates:

        def none(db):
            svc.no_results(db, request_id, used)
            db.commit()

        await ctx.run_db(none)
        await ctx.log("No results: nothing on the network matches (try a manual search)")
        await ctx.progress(100, "No results")
        return

    manual = data["mode"] == MODE_MANUAL

    def save(db):
        svc.store_candidates(db, request_id, candidates, used)
        request = db.get(DownloadRequest, request_id)
        if manual:
            svc.awaiting_choice(db, request_id)
        else:
            svc.choose_candidate(db, request, 0)
            engine.enqueue(
                db,
                "download_monitor",
                {"request_id": request_id},
                dedupe_key=svc.monitor_dedupe_key(request_id),
                created_by=None,
                parent_job_id=ctx.job_id,
            )
        db.commit()

    await ctx.run_db(save)
    best = candidates[0]
    await ctx.log(
        f"{len(candidates)} candidate(s); best: {best.username} / {best.directory} "
        f"(score {best.score}, {best.quality}, {best.track_count} track(s))"
    )
    await ctx.log("Waiting for a choice" if manual else "Starting the download")
    await ctx.progress(100, "Waiting for a choice" if manual else "Candidate found")
