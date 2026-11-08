import asyncio

import pytest
from sqlalchemy import select

from app.config import Settings
from app.jobs import engine
from app.jobs.errors import PermanentJobError
from app.jobs.registry import job_handler
from app.jobs.worker import JobWorker
from app.models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    Job,
    JobLog,
)

# ------------------------------------------------------------------ test handlers

_calls = {"running": 0, "max": 0, "flaky": 0}
_gate = {}


@job_handler("test.limited", queue="q.limited")
async def _limited(ctx):
    _calls["running"] += 1
    _calls["max"] = max(_calls["max"], _calls["running"])
    await asyncio.sleep(0.05)
    _calls["running"] -= 1


@job_handler("test.blocker", queue="q.a")
async def _blocker(ctx):
    await _gate["open"].wait()


@job_handler("test.quick", queue="q.b")
async def _quick(ctx):
    await ctx.progress(50, "half")


@job_handler("test.flaky", max_attempts=3, backoff_base=0.01)
async def _flaky(ctx):
    if ctx.attempt < 3:
        raise RuntimeError(f"flaky failure {ctx.attempt}")


@job_handler("test.broken", max_attempts=2, backoff_base=0.01)
async def _broken(ctx):
    raise RuntimeError("boom")


@job_handler("test.permanent", max_attempts=5)
async def _permanent(ctx):
    raise PermanentJobError("not worth retrying")


@job_handler("test.leaky", max_attempts=1)
async def _leaky(ctx):
    await ctx.log(f"calling with key {ctx.settings.slskd_api_key}")
    raise RuntimeError(f"401 for key {ctx.settings.slskd_api_key}")


# ------------------------------------------------------------------------ helpers


@pytest.fixture
def wsettings(settings):
    return settings.model_copy(
        update={"job_poll_interval": 0.02, "job_queue_concurrency": "q.limited:2"}
    )


@pytest.fixture
async def worker(wsettings, session_factory):
    _calls.update(running=0, max=0)
    w = JobWorker(wsettings, session_factory)
    await w.start()
    yield w
    await w.stop()


def fetch(factory, job_id):
    with factory() as db:
        job = db.get(Job, job_id)
        db.expunge(job)
        return job


def logs(factory, job_id):
    with factory() as db:
        return [m for (m,) in db.execute(select(JobLog.message).where(JobLog.job_id == job_id))]


def enqueue(factory, type, payload=None, **kw):
    with factory() as db:
        return engine.enqueue(db, type, payload, **kw).id


async def wait_for_status(factory, job_id, status, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        job = await asyncio.to_thread(fetch, factory, job_id)
        if job.status == status:
            return job
        await asyncio.sleep(0.01)
    job = await asyncio.to_thread(fetch, factory, job_id)
    raise AssertionError(f"job {job_id} is {job.status}, expected {status} ({job.error})")


# ------------------------------------------------------------------------- tests


def test_queue_concurrency_parsing_and_musicbrainz_cap():
    s = Settings(_env_file=None, job_queue_concurrency="default:3, slskd:2,musicbrainz:5,bad,x:0")
    assert s.queue_concurrency == {"default": 3, "slskd": 2, "musicbrainz": 1}


async def test_a_stepped_job_completes_with_progress_and_logs(worker, session_factory):
    job_id = enqueue(session_factory, "test.steps", {"steps": 10, "step_seconds": 0.01})
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    assert job.progress_pct == 100.0 and job.step == "Step 10/10"
    assert job.attempts == 1 and job.finished_at is not None
    messages = logs(session_factory, job_id)
    assert messages[0] == "Queued" and messages[-1] == "Completed"
    assert "Reached step 5/10" in messages and "Reached step 10/10" in messages


async def test_queue_concurrency_is_respected(worker, session_factory):
    ids = [enqueue(session_factory, "test.limited") for _ in range(6)]
    worker.notify()
    for job_id in ids:
        await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    assert _calls["max"] == 2


async def test_queues_are_independent(worker, session_factory):
    _gate["open"] = asyncio.Event()
    blocked = enqueue(session_factory, "test.blocker")
    await wait_for_status(session_factory, blocked, STATUS_RUNNING)
    quick = enqueue(session_factory, "test.quick")
    worker.notify()
    await wait_for_status(session_factory, quick, STATUS_SUCCEEDED)  # not stuck behind q.a
    assert fetch(session_factory, blocked).status == STATUS_RUNNING
    _gate["open"].set()
    await wait_for_status(session_factory, blocked, STATUS_SUCCEEDED)


async def test_failed_attempts_are_retried_with_backoff(worker, session_factory):
    job_id = enqueue(session_factory, "test.flaky")
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    assert job.attempts == 3 and job.error is None
    retries = [m for m in logs(session_factory, job_id) if "Retrying in" in m]
    assert len(retries) == 2 and "flaky failure 1" in retries[0]


async def test_job_fails_for_good_after_max_attempts(worker, session_factory):
    job_id = enqueue(session_factory, "test.broken")
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
    assert job.attempts == 2 and job.error == "RuntimeError: boom"
    assert logs(session_factory, job_id)[-1] == "Failed: RuntimeError: boom"


async def test_permanent_error_is_not_retried(worker, session_factory):
    job_id = enqueue(session_factory, "test.permanent")
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
    assert job.attempts == 1 and job.error == "not worth retrying"


async def test_unknown_job_type_fails_with_clear_message(worker, session_factory):
    with session_factory() as db:
        ghost = Job(type="ghost", queue="default", status=STATUS_QUEUED, max_attempts=3)
        db.add(ghost)
        db.commit()
        ghost_id = ghost.id
    worker.notify()
    job = await wait_for_status(session_factory, ghost_id, STATUS_FAILED)
    assert job.error == "Unknown job type: ghost"


async def test_running_job_can_be_cancelled_cooperatively(worker, session_factory):
    job_id = enqueue(session_factory, "test.steps", {"steps": 500, "step_seconds": 0.01})
    worker.notify()
    await wait_for_status(session_factory, job_id, STATUS_RUNNING)
    await asyncio.sleep(0.1)
    with session_factory() as db:
        assert engine.request_cancel(db, job_id)
    job = await wait_for_status(session_factory, job_id, STATUS_CANCELLED)
    assert 0 < job.progress_pct < 100
    assert logs(session_factory, job_id)[-1] == "Cancelled"


async def test_queued_job_cancelled_before_start_never_runs(wsettings, session_factory):
    job_id = enqueue(session_factory, "test.steps", {"steps": 3, "step_seconds": 0.01})
    with session_factory() as db:
        engine.request_cancel(db, job_id)
    worker = JobWorker(wsettings, session_factory)
    await worker.start()
    await asyncio.sleep(0.1)
    await worker.stop()
    job = fetch(session_factory, job_id)
    assert job.status == STATUS_CANCELLED and job.attempts == 0


async def test_manual_retry_runs_again(worker, session_factory):
    job_id = enqueue(session_factory, "test.permanent")
    worker.notify()
    await wait_for_status(session_factory, job_id, STATUS_FAILED)
    with session_factory() as db:
        engine.retry(db, job_id)
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
    assert job.attempts == 1  # a fresh budget: the manual retry ran once more


async def test_notify_wakes_idle_workers(wsettings, session_factory):
    slow = wsettings.model_copy(update={"job_poll_interval": 30.0})
    worker = JobWorker(slow, session_factory)
    await worker.start()
    await asyncio.sleep(0.1)  # loops are now sleeping for up to 30 s
    job_id = enqueue(session_factory, "test.quick")
    worker.notify()
    await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED, timeout=2)
    await worker.stop()


async def test_secrets_never_reach_logs_or_errors(wsettings, session_factory):
    settings = wsettings.model_copy(update={"slskd_api_key": "super-secret-key-123"})
    worker = JobWorker(settings, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "test.leaky")
    worker.notify()
    job = await wait_for_status(session_factory, job_id, STATUS_FAILED)
    await worker.stop()
    assert "super-secret-key-123" not in (job.error or "")
    assert "***" in job.error
    assert all("super-secret-key-123" not in m for m in logs(session_factory, job_id))


async def test_logs_are_trimmed_at_the_end(wsettings, session_factory):
    settings = wsettings.model_copy(update={"job_log_max_lines": 5})
    worker = JobWorker(settings, session_factory)
    await worker.start()
    job_id = enqueue(session_factory, "test.steps", {"steps": 20, "step_seconds": 0.001})
    worker.notify()
    await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    messages = logs(session_factory, job_id)
    assert len(messages) == 5 and messages[-1] == "Completed"


# --------------------------------------------------------------- restart / recovery


async def test_interrupted_job_resumes_from_checkpoint_after_crash(wsettings, session_factory):
    """Simulate a crash: the DB says 'running' with a saved checkpoint, nobody is running it."""
    job_id = enqueue(session_factory, "test.steps", {"steps": 6, "step_seconds": 0.01})
    with session_factory() as db:
        engine.claim_next(db, "default")
        engine.save_state(db, job_id, {"done": 4})
    worker = JobWorker(wsettings, session_factory)
    assert await worker.start() == 1
    job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED)
    await worker.stop()
    assert job.attempts == 1  # the interruption did not consume an attempt
    messages = logs(session_factory, job_id)
    assert "Resumed after restart" in messages
    assert "Resuming from step 4/6" in messages
    assert "Reached step 4/6" not in messages  # steps 1-4 were not redone


async def test_graceful_shutdown_requeues_and_next_start_finishes(wsettings, session_factory):
    job_id = enqueue(session_factory, "test.steps", {"steps": 40, "step_seconds": 0.02})
    first = JobWorker(wsettings, session_factory)
    await first.start()
    await wait_for_status(session_factory, job_id, STATUS_RUNNING)
    await asyncio.sleep(0.25)
    await first.stop()
    interrupted = fetch(session_factory, job_id)
    assert interrupted.status == STATUS_QUEUED
    assert "Interrupted by shutdown" in logs(session_factory, job_id)

    second = JobWorker(wsettings, session_factory)
    await second.start()
    job = await wait_for_status(session_factory, job_id, STATUS_SUCCEEDED, timeout=10)
    await second.stop()
    assert any(m.startswith("Resuming from step") for m in logs(session_factory, job_id))
    assert job.progress_pct == 100.0


async def test_pending_cancellation_wins_over_recovery(wsettings, session_factory):
    job_id = enqueue(session_factory, "test.steps", {"steps": 5, "step_seconds": 0.01})
    with session_factory() as db:
        engine.claim_next(db, "default")
        engine.request_cancel(db, job_id)
    worker = JobWorker(wsettings, session_factory)
    await worker.start()
    await asyncio.sleep(0.1)
    await worker.stop()
    assert fetch(session_factory, job_id).status == STATUS_CANCELLED


async def test_a_busy_database_does_not_end_the_queue_loop(settings, session_factory, monkeypatch):
    """ "database is locked" while claiming or finishing a job is transient: the loop lives on."""
    from sqlalchemy.exc import OperationalError

    from app.jobs import engine as eng
    from app.jobs import worker as worker_mod
    from app.jobs.worker import JobWorker

    monkeypatch.setattr(worker_mod, "DB_WRITE_PAUSE", 0.01)
    real_claim, real_success = eng.claim_next, eng.finish_success
    state = {"claim": 0, "success": 0}

    def flaky_claim(db, queue):
        state["claim"] += 1
        if state["claim"] <= 2:
            raise OperationalError("claim", {}, Exception("database is locked"))
        return real_claim(db, queue)

    def flaky_success(db, job_id, **kw):
        state["success"] += 1
        if state["success"] <= 2:
            raise OperationalError("finish", {}, Exception("database is locked"))
        return real_success(db, job_id, **kw)

    monkeypatch.setattr(eng, "claim_next", flaky_claim)
    monkeypatch.setattr(eng, "finish_success", flaky_success)
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), session_factory)
    await worker.start()
    first = enqueue(session_factory, "test.steps", {"steps": 1})
    worker.notify()
    await wait_for_status(session_factory, first, "succeeded", timeout=10)
    # and the queue keeps working for the next job
    second = enqueue(session_factory, "test.steps", {"steps": 1})
    worker.notify()
    await wait_for_status(session_factory, second, "succeeded", timeout=10)
    await worker.stop()
    assert state["claim"] > 2 and state["success"] > 2
