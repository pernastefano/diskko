"""Download the chosen candidate through SLSKD and follow it until it is done.

The request is the source of truth, so a restart simply resumes: the files and their SLSKD
transfer ids are in the database. What can go wrong is handled here:

* the user is offline, or SLSKD refuses a file: the file is asked again (a few times), then the
  candidate is given up for the next one;
* a transfer fails (rejected, timed out, errored...): same;
* nothing arrives for `slskd_stall_minutes` while a transfer is running: the candidate is given
  up; staying in the remote queue longer than `slskd_queue_minutes`, or taking longer than
  `slskd_max_hours` altogether, likewise;
* the request is cancelled: the transfers are removed from SLSKD and the job stops.
"""

import asyncio
import time
from pathlib import Path

from app.integrations.slskd_client import (
    SlskdAuthError,
    SlskdClient,
    SlskdError,
    SlskdNotConfigured,
    SlskdUserOffline,
    Transfer,
    is_failed,
    is_queued,
    is_running,
    is_succeeded,
)
from app.jobs import engine
from app.jobs.context import JobContext
from app.jobs.errors import JobCancelled, PermanentJobError
from app.jobs.handlers.postprocess import postprocess_dedupe_key
from app.jobs.registry import job_handler
from app.models import (
    REQ_CANDIDATE_FOUND,
    REQ_DOWNLOADED,
    REQ_DOWNLOADING,
    REQ_FAILED,
    DownloadRequest,
)
from app.services import downloads as svc
from app.services.candidate_selection import group_by_subdir

MAX_ATTEMPTS = 3
ATTACH_WAIT = 90.0  # how long SLSKD may take to list the transfers it was just asked for


class _GiveUp(Exception):
    """The candidate cannot be downloaded: the reason goes to the log and the next one is tried."""


def _db(func):
    def run(db):
        result = func(db)
        db.commit()
        return result

    return run


@job_handler("download_monitor", queue="slskd", max_attempts=MAX_ATTEMPTS, backoff_base=60)
async def download_monitor(ctx: JobContext) -> None:
    request_id = int(ctx.payload["request_id"])
    settings = ctx.settings
    try:
        client = SlskdClient(settings)
    except SlskdNotConfigured as exc:
        raise PermanentJobError(str(exc)) from exc

    def fail_request(message: str):
        return _db(lambda db: svc.set_status(db, request_id, REQ_FAILED, error=message))

    try:
        async with client:
            await _run(ctx, client, request_id)
    except SlskdAuthError as exc:
        await ctx.run_db(fail_request(str(exc)))
        raise PermanentJobError(str(exc)) from exc
    except SlskdError as exc:
        if ctx.attempt >= MAX_ATTEMPTS:
            await ctx.run_db(fail_request(f"SLSKD: {exc}"))
        raise


async def _status(ctx: JobContext, request_id: int):
    def read(db):
        request = db.get(DownloadRequest, request_id)
        return (request.status, request.candidate_index) if request else (None, -1)

    return await ctx.run_db(read)


async def _run(ctx: JobContext, client: SlskdClient, request_id: int) -> None:
    status, _ = await _status(ctx, request_id)
    if status is None:
        raise PermanentJobError(f"download request {request_id} not found")
    if status not in (REQ_CANDIDATE_FOUND, REQ_DOWNLOADING):
        await ctx.log(f"Skipped: the request is {status}")
        return

    while True:
        status, index = await _status(ctx, request_id)
        if status not in (REQ_CANDIDATE_FOUND, REQ_DOWNLOADING):
            await ctx.log(f"Stopping: the request is {status}")
            await _remove_transfers(ctx, client, request_id)
            return
        try:
            finished = await _download_candidate(ctx, client, request_id, index)
        except JobCancelled:
            await _remove_transfers(ctx, client, request_id)
            raise
        except _GiveUp as exc:
            reason = str(exc)
            await ctx.log(f"Giving up on this candidate: {reason}", "warning")
            await _remove_transfers(ctx, client, request_id)
            more = await ctx.run_db(
                _db(lambda db, why=reason: svc.next_candidate(db, request_id, why))
            )
            if not more:
                await ctx.log("No candidate left: the request failed", "error")
                return
            await ctx.log("Trying the next candidate")
            continue
        if finished:
            status, _ = await _status(ctx, request_id)
            if status != REQ_DOWNLOADED:  # cancelled meanwhile: take the transfers out of SLSKD
                await _remove_transfers(ctx, client, request_id)
            else:  # the files are here: check, tag and import them
                await ctx.run_db(
                    lambda db: engine.enqueue(
                        db,
                        "postprocess",
                        {"request_id": request_id},
                        dedupe_key=postprocess_dedupe_key(request_id),
                        parent_job_id=ctx.job_id,
                    )
                )
                await ctx.log("Download complete: the import was queued")
            return


async def _download_candidate(
    ctx: JobContext, client: SlskdClient, request_id: int, index: int
) -> bool:
    """Download one candidate. True when finished (or stopped), raises _GiveUp when it failed."""
    settings = ctx.settings

    def load(db):
        request = db.get(DownloadRequest, request_id)
        candidate = svc.current_candidate(request)
        return candidate, svc.snapshot(db, request_id), request.dest_dir

    candidate, files, dest_dir = await ctx.run_db(load)
    if candidate is None:
        raise PermanentJobError("the request has no chosen candidate")
    await ctx.log(
        f"Downloading from {candidate.username}: {candidate.directory} "
        f"({candidate.track_count} file(s), candidate {index + 1})"
    )

    def begin(db):
        request = db.get(DownloadRequest, request_id)
        if not svc.snapshot(db, request_id):
            svc.create_files(db, request, candidate)
        svc.downloading(db, request_id)

    await ctx.run_db(_db(begin))
    files = await ctx.run_db(lambda db: svc.snapshot(db, request_id))

    pending = [f for f in files if f.transfer_id is None]
    if pending:
        await _enqueue(ctx, client, request_id, candidate, pending)

    started = time.monotonic()
    last_bytes, last_move = -1, time.monotonic()
    queue_since = time.monotonic()
    stall = settings.slskd_stall_minutes * 60
    queue_limit = settings.slskd_queue_minutes * 60
    overall = settings.slskd_max_hours * 3600

    while True:
        await ctx.checkpoint()
        status, _ = await _status(ctx, request_id)
        if status != REQ_DOWNLOADING:
            return True  # cancelled (or moved on) meanwhile: the caller cleans up
        transfers = await client.list_downloads()
        progress = await ctx.run_db(
            _db(lambda db, t=transfers: svc.update_files(db, request_id, t))
        )
        files = await ctx.run_db(lambda db: svc.snapshot(db, request_id))
        await ctx.progress(progress, f"{progress:.0f}% downloaded")

        if files and all(is_succeeded(f.state) for f in files):
            await _verify_on_disk(ctx, request_id, dest_dir, files)
            await ctx.run_db(_db(lambda db: svc.mark_downloaded(db, request_id)))
            await ctx.log(f"Downloaded {len(files)} file(s): the album is in the downloads folder")
            await ctx.progress(100, "Downloaded")
            return True

        failed = [f for f in files if is_failed(f.state)]
        by_id = {t.id: t for t in transfers}
        # a transfer that SLSKD no longer lists (removed behind our back) counts as failed
        failed += [
            f
            for f in files
            if f.transfer_id and f.transfer_id not in by_id and not is_succeeded(f.state)
        ]
        if failed:
            retry = [f for f in failed if f.attempts <= settings.slskd_file_retries]
            if len(retry) < len(failed):
                names = ", ".join(
                    sorted({Path(f.filename.replace("\\", "/")).name for f in failed})[:3]
                )
                raise _GiveUp(f"{len(failed)} file(s) could not be downloaded ({names})")
            await ctx.log(f"{len(retry)} file(s) failed: asking for them again", "warning")
            for f in retry:
                if f.transfer_id:
                    await client.cancel_transfer(f.username, f.transfer_id, remove=True)
                await ctx.run_db(_db(lambda db, i=f.id: svc.reset_file(db, i, error="retry")))
            files = await ctx.run_db(lambda db: svc.snapshot(db, request_id))
            await _enqueue(
                ctx, client, request_id, candidate, [f for f in files if f.transfer_id is None]
            )
            last_bytes, last_move = -1, time.monotonic()
            continue

        now = time.monotonic()
        total = sum(f.bytes_transferred for f in files)
        if total != last_bytes:
            last_bytes, last_move = total, now
        running = [f for f in files if is_running(f.state)]
        if running and now - last_move > stall:
            raise _GiveUp(f"no data received for {settings.slskd_stall_minutes:g} minute(s)")
        if running:
            queue_since = now  # something is moving: the remote queue is not the problem
        elif (
            all(is_queued(f.state) or is_succeeded(f.state) for f in files)
            and now - queue_since > queue_limit
        ):
            raise _GiveUp(
                f"still in the remote queue after {settings.slskd_queue_minutes:g} minute(s)"
            )
        if now - started > overall:
            raise _GiveUp(f"took longer than {settings.slskd_max_hours:g} hour(s)")
        await asyncio.sleep(settings.slskd_poll_seconds)


async def _enqueue(ctx, client, request_id, candidate, files) -> None:
    """Ask SLSKD for these files (one batch per disc folder) and note the transfer ids."""
    dest_dir = await ctx.run_db(lambda db: db.get(DownloadRequest, request_id).dest_dir)
    wanted = {(f.username, f.filename) for f in files}
    refused: dict[str, str] = {}
    for subdir, group in group_by_subdir(candidate).items():
        batch = [
            (f["filename"], f["size"])
            for f in group
            if (candidate.username, f["filename"]) in wanted
        ]
        if not batch:
            continue
        try:
            result = await client.enqueue(
                candidate.username,
                batch,
                svc.destination(dest_dir, request_id, subdir),
                external_id=f"diskko-{request_id}",
            )
        except SlskdUserOffline as exc:
            raise _GiveUp(str(exc)) from exc
        refused.update(result.failures)
    if refused:
        await ctx.log(f"SLSKD refused {len(refused)} file(s)", "warning")

    # the transfers are created in the background: wait until they are listed
    end = time.monotonic() + ATTACH_WAIT
    while True:
        transfers = await client.list_downloads()
        await ctx.run_db(_db(lambda db, t=transfers: svc.attach_transfers(db, request_id, t)))
        left = await ctx.run_db(
            lambda db: [f for f in svc.snapshot(db, request_id) if f.transfer_id is None]
        )
        missing = [f for f in left if f.filename not in refused]
        if not missing or time.monotonic() > end:
            break
        await asyncio.sleep(1.0)
    if left:
        await ctx.log(f"{len(left)} file(s) have no transfer in SLSKD", "warning")

    def mark(db):
        from app.models import DownloadFile

        for f in left:
            row = db.get(DownloadFile, f.id)
            row.state = "Completed, Rejected"
            row.error = refused.get(f.filename, "not queued by SLSKD")[:200]

    await ctx.run_db(_db(mark))


async def _remove_transfers(ctx: JobContext, client: SlskdClient, request_id: int) -> None:
    """Take the transfers of a request out of SLSKD (cancelled or given up)."""
    files = await ctx.run_db(lambda db: svc.snapshot(db, request_id))
    for f in files:
        if f.transfer_id:
            try:
                await client.cancel_transfer(f.username, f.transfer_id, remove=True)
            except SlskdError as exc:
                await ctx.log(f"Could not remove a transfer: {exc}", "warning")


async def _verify_on_disk(ctx: JobContext, request_id: int, dest_dir: str | None, files) -> None:
    """If the downloads folder is mounted here, check that the files are really there."""
    root = ctx.settings.downloads_dir
    if not dest_dir or not str(root).strip() or not Path(root).is_dir():
        await ctx.log("The downloads folder is not reachable from here: files not checked")
        return
    missing = 0
    for f in files:
        name = Path(f.filename.replace("\\", "/")).name
        path = Path(root) / svc.destination(dest_dir, request_id, f.subdir) / name
        if not path.is_file():
            missing += 1
    if missing:
        await ctx.log(
            f"{missing} of {len(files)} file(s) are not in the downloads folder", "warning"
        )
    else:
        await ctx.log("All the files are in the downloads folder")


__all__ = ["Transfer", "download_monitor"]
