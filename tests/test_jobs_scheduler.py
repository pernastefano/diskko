import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.jobs import engine
from app.jobs.scheduler import SCHEDULES, JobScheduler, Schedule, enqueue_scheduled
from app.jobs.worker import JobWorker
from app.main import create_app
from app.models import STATUS_SUCCEEDED, Job, utcnow
from tests.test_jobs_worker import enqueue, fetch, wait_for_status


def test_housekeeping_is_scheduled_daily():
    assert Schedule("housekeeping", "30 3 * * *") in SCHEDULES


def test_enqueue_scheduled_does_not_duplicate_active_runs(session_factory):
    schedule = Schedule("housekeeping", "30 3 * * *")
    first = enqueue_scheduled(session_factory, schedule)
    assert enqueue_scheduled(session_factory, schedule) == first
    with session_factory() as db:
        assert db.scalar(select(func.count(Job.id))) == 1
        assert db.get(Job, first).dedupe_key == "scheduled:housekeeping"


async def test_scheduler_registers_and_fires_jobs(settings, session_factory):
    notified = []
    schedules = [Schedule("housekeeping", "0 4 * * *"), Schedule("demo", "*/5 * * * *")]
    scheduler = JobScheduler(
        settings, session_factory, notify=lambda: notified.append(1), schedules=schedules
    )
    scheduler.start()
    try:
        assert sorted(scheduler.job_ids()) == ["scheduled:demo", "scheduled:housekeeping"]
        await scheduler._fire(schedules[0])
        await scheduler._fire(schedules[0])  # second trigger while the first is still queued
    finally:
        scheduler.shutdown()
    with session_factory() as db:
        assert db.scalar(select(func.count(Job.id)).where(Job.type == "housekeeping")) == 1
    assert len(notified) == 2


async def test_unknown_timezone_falls_back_to_utc(settings, session_factory):
    s = settings.model_copy(update={"tz": "Not/AZone"})
    scheduler = JobScheduler(s, session_factory, schedules=[])
    assert str(scheduler._scheduler.timezone) == "UTC"


async def test_housekeeping_job_purges_old_jobs(settings, session_factory):
    wsettings = settings.model_copy(update={"job_poll_interval": 0.02, "job_retention_days": 30})
    with session_factory() as db:
        old = engine.enqueue(db, "demo", {"steps": 1, "step_seconds": 0})
        engine.claim_next(db, "default")
        engine.finish_success(db, old.id)
        job = db.get(Job, old.id)
        job.finished_at = utcnow() - timedelta(days=45)
        db.commit()
        old_id = old.id
    worker = JobWorker(wsettings, session_factory)
    await worker.start()
    hk = enqueue(session_factory, "housekeeping")
    worker.notify()
    await wait_for_status(session_factory, hk, STATUS_SUCCEEDED)
    await worker.stop()
    with session_factory() as db:
        assert db.get(Job, old_id) is None
    assert fetch(session_factory, hk).status == STATUS_SUCCEEDED


def test_app_lifespan_starts_and_stops_workers(settings):
    app = create_app(settings.model_copy(update={"jobs_enabled": True, "job_poll_interval": 0.05}))
    with TestClient(app):
        assert app.state.worker is not None and app.state.scheduler is not None
        assert "scheduled:housekeeping" in app.state.scheduler.job_ids()
    assert not app.state.scheduler._scheduler.running


def test_app_restart_resumes_interrupted_job(settings):
    """The acceptance criterion: a job interrupted mid-run finishes after the app restarts."""
    live = settings.model_copy(update={"jobs_enabled": True, "job_poll_interval": 0.05})

    # First run: the app is down, a job was left 'running' with a checkpoint (e.g. a crash).
    app = create_app(settings)  # workers disabled
    with TestClient(app), app.state.session_factory() as db:
        job = engine.enqueue(db, "demo", {"steps": 6, "step_seconds": 0.01})
        engine.claim_next(db, "default")
        engine.save_state(db, job.id, {"done": 3})
        job_id = job.id

    # Second run: startup recovers it and the worker completes it.
    app = create_app(live)
    with TestClient(app):
        for _ in range(200):
            with app.state.session_factory() as db:
                if db.get(Job, job_id).status == STATUS_SUCCEEDED:
                    break
            asyncio.run(asyncio.sleep(0.02))
        with app.state.session_factory() as db:
            done = db.get(Job, job_id)
            assert done.status == STATUS_SUCCEEDED and done.attempts == 1
            assert done.progress_pct == 100.0
