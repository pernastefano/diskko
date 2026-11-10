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
from app.jobs.errors import JobCancelled, PermanentJobError, RetryLater
from app.jobs.registry import job_handler
from app.models import (
    MODE_MANUAL,
    REQ_CANCELLED,
    REQ_FAILED,
    REQ_QUEUED,
    REQ_SEARCHING,
    DownloadRequest,
)
from app.services import downloads as svc
from app.services.candidate_selection import SearchStep, search_plan, select_candidates
from app.services.quality_profiles import load_profiles

MAX_ATTEMPTS = 2
PAUSE_BETWEEN_SEARCHES = 3.0  # the Soulseek server does not like a search storm
BROAD_RESPONSE_LIMIT = 300  # a broad search ("Adams") has many answers; the name check picks
BROAD_FILE_LIMIT = 30000
POOL = (
    40  # candidates kept for a manual choice: the page shows the first few and the rest on demand
)
BROWSE_LIMIT = 80  # folders a free search keeps
TRIED_SEPARATOR = " | "  # the texts tried, kept in the request when nothing was found


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
    browse = bool(ctx.payload.get("browse"))  # a search by hand, with the user's own text
    typed = " ".join(str(ctx.payload.get("query") or "").split())
    data = await ctx.run_db(_load(request_id))
    if data is None:
        raise PermanentJobError(f"download request {request_id} not found")
    if data["status"] not in (REQ_QUEUED, REQ_SEARCHING):
        await ctx.log(f"Skipped: the request is {data['status']}")
        if data["status"] == REQ_FAILED:  # a retry of this job cannot revive it
            raise PermanentJobError("The request failed: search again from the Requests page")
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
    if browse and typed:
        plan = [SearchStep(typed, True)]
    elif data["override"]:
        plan = [SearchStep(data["override"])]
    else:
        plan = search_plan(artist, album)
    profiles = await ctx.run_db(load_profiles)
    settings = ctx.settings
    timeout = settings.slskd_search_timeout

    candidates = []
    used = plan[-1].text
    tried: list[str] = []
    try:
        async with client:
            state = await client.server_state()
            if not (state["connected"] and state["logged_in"]):
                raise SlskdError("SLSKD is not connected to the Soulseek network")
            for i, step in enumerate(plan):
                query = step.text
                tried.append(query)
                await ctx.checkpoint(force=True)
                await ctx.progress(100 * i / len(plan), f"Searching: {query}")
                await ctx.log(f"Searching for {query!r}" + (" (broad)" if step.broad else ""))
                limits = (
                    {"response_limit": BROAD_RESPONSE_LIMIT, "file_limit": BROAD_FILE_LIMIT}
                    if step.broad
                    else {}
                )
                search_id = await client.create_search(query, timeout_s=timeout, **limits)
                try:
                    info = await client.wait_for_search(search_id, hard_timeout=timeout * 3 + 20)
                    responses = await client.get_responses(search_id)
                finally:
                    await client.delete_search(search_id)
                await ctx.log(
                    f"{len(responses)} user(s) answered ({info.file_count} file(s), {info.state})"
                )
                candidates = select_candidates(
                    responses,
                    wanted,
                    profiles,
                    limit=BROWSE_LIMIT if browse else max(settings.slskd_candidates, POOL),
                    browse=browse,
                )
                used = query
                if candidates:
                    break
                await ctx.log("No suitable folder in the answers")
                if i < len(plan) - 1:
                    await asyncio.sleep(PAUSE_BETWEEN_SEARCHES)
    except SlskdAuthError as exc:
        await ctx.run_db(fail(str(exc)))
        raise PermanentJobError(str(exc)) from exc
    except SlskdError as exc:
        if ctx.attempt >= MAX_ATTEMPTS:
            await ctx.run_db(fail(f"SLSKD: {exc}"))
        raise
    except (JobCancelled, RetryLater, PermanentJobError):
        raise
    except Exception as exc:  # noqa: BLE001 - whatever it is, the request must not hang
        if ctx.attempt >= MAX_ATTEMPTS:
            await ctx.run_db(fail(f"Search failed ({type(exc).__name__}): see the job log"))
        raise

    if not candidates:

        def none(db):
            svc.no_results(db, request_id, TRIED_SEPARATOR.join(tried)[:255])
            db.commit()

        await ctx.run_db(none)
        await ctx.log("No results: nothing on the network matches (try a manual search)")
        await ctx.progress(100, "No results")
        return

    manual = browse or data["mode"] == MODE_MANUAL  # a search by hand always waits for you

    def save(db):
        svc.store_candidates(db, request_id, candidates, used)
        request = db.get(DownloadRequest, request_id)
        if request is None or request.status == REQ_CANCELLED:
            return  # cancelled while the search ran: no download must start
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
