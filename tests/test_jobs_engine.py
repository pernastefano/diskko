import random
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.jobs import engine
from app.jobs.errors import JobError
from app.jobs.registry import job_handler
from app.models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    Job,
    JobLog,
    utcnow,
)


@job_handler("test.noop")
async def _noop(ctx):
    return None


@job_handler("test.other", queue="other", max_attempts=5, backoff_base=10)
async def _other(ctx):
    return None


def _logs(db, job_id):
    return [m for (m,) in db.execute(select(JobLog.message).where(JobLog.job_id == job_id))]


def test_enqueue_uses_spec_defaults(jdb):
    job = engine.enqueue(jdb, "test.other", {"a": 1}, created_by=None)
    assert job.status == STATUS_QUEUED
    assert (job.queue, job.max_attempts, job.attempts) == ("other", 5, 0)
    assert job.payload_json == '{"a": 1}'
    assert _logs(jdb, job.id) == ["Queued"]


def test_enqueue_unknown_type_rejected(jdb):
    with pytest.raises(JobError):
        engine.enqueue(jdb, "no.such.type")


def test_dedupe_returns_existing_active_job(jdb):
    first = engine.enqueue(jdb, "test.noop", dedupe_key="k1")
    second = engine.enqueue(jdb, "test.noop", dedupe_key="k1")
    assert second.id == first.id
    assert jdb.scalar(select(func.count(Job.id))) == 1
    # different key, or no key at all, is a new job
    assert engine.enqueue(jdb, "test.noop", dedupe_key="k2").id != first.id
    assert engine.enqueue(jdb, "test.noop").id != engine.enqueue(jdb, "test.noop").id


def test_dedupe_key_is_free_again_after_finish(jdb):
    first = engine.enqueue(jdb, "test.noop", dedupe_key="k1")
    claimed = engine.claim_next(jdb, "default")
    assert claimed.id == first.id
    assert engine.enqueue(jdb, "test.noop", dedupe_key="k1").id == first.id  # still running
    engine.finish_success(jdb, first.id)
    assert engine.enqueue(jdb, "test.noop", dedupe_key="k1").id != first.id


def test_database_enforces_dedupe_uniqueness(jdb):
    from sqlalchemy.exc import IntegrityError

    jdb.add(Job(type="test.noop", queue="default", dedupe_key="dup", status=STATUS_QUEUED))
    jdb.commit()
    jdb.add(Job(type="test.noop", queue="default", dedupe_key="dup", status=STATUS_RUNNING))
    with pytest.raises(IntegrityError):
        jdb.commit()
    jdb.rollback()


def test_claim_is_fifo_per_queue(jdb):
    a = engine.enqueue(jdb, "test.noop")
    b = engine.enqueue(jdb, "test.noop")
    other = engine.enqueue(jdb, "test.other")
    assert engine.claim_next(jdb, "default").id == a.id
    assert engine.claim_next(jdb, "default").id == b.id
    assert engine.claim_next(jdb, "default") is None
    assert engine.claim_next(jdb, "other").id == other.id


def test_claim_sets_running_and_counts_attempt(jdb):
    job = engine.enqueue(jdb, "test.noop")
    claimed = engine.claim_next(jdb, "default")
    assert claimed.status == STATUS_RUNNING
    assert claimed.attempts == 1 and claimed.started_at is not None
    assert "Started (attempt 1/3)" in _logs(jdb, job.id)


def test_claim_respects_next_run_at(jdb):
    job = engine.enqueue(jdb, "test.noop", delay=60)
    assert engine.claim_next(jdb, "default") is None
    later = utcnow() + timedelta(seconds=61)
    assert engine.claim_next(jdb, "default", now=later).id == job.id


def test_claim_cannot_take_the_same_job_twice(session_factory):
    with session_factory() as s1, session_factory() as s2:
        engine.enqueue(s1, "test.noop")
        first = engine.claim_next(s1, "default")
        second = engine.claim_next(s2, "default")
        assert first is not None and second is None


def test_progress_is_clamped_and_step_stored(jdb):
    job = engine.enqueue(jdb, "test.noop")
    engine.update_progress(jdb, job.id, 150, "x" * 400)
    jdb.refresh(job)
    assert job.progress_pct == 100.0 and len(job.step) == 255
    engine.update_progress(jdb, job.id, -5)
    jdb.refresh(job)
    assert job.progress_pct == 0.0


def test_logs_after_cursor(jdb):
    job = engine.enqueue(jdb, "test.noop")
    for i in range(3):
        engine.add_log(jdb, job.id, "info", f"line {i}")
    everything = engine.logs_after(jdb, job.id)
    assert [x.message for x in everything] == ["Queued", "line 0", "line 1", "line 2"]
    tail = engine.logs_after(jdb, job.id, after=everything[1].id)
    assert [x.message for x in tail] == ["line 1", "line 2"]
    assert engine.logs_after(jdb, job.id, after=everything[-1].id) == []


def test_trim_logs_keeps_newest(jdb):
    job = engine.enqueue(jdb, "test.noop")
    for i in range(10):
        engine.add_log(jdb, job.id, "info", f"line {i}")
    removed = engine.trim_logs(jdb, job.id, 4)
    assert removed == 7
    assert [m for m in _logs(jdb, job.id)] == ["line 6", "line 7", "line 8", "line 9"]
    assert engine.trim_logs(jdb, job.id, 100) == 0


def test_state_roundtrip(jdb):
    job = engine.enqueue(jdb, "test.noop")
    assert engine.get_state(jdb, job.id) == {}
    engine.save_state(jdb, job.id, {"step": 7})
    assert engine.get_state(jdb, job.id) == {"step": 7}


def test_failure_with_retry_requeues_with_delay(jdb):
    job = engine.enqueue(jdb, "test.noop")
    engine.claim_next(jdb, "default")
    engine.finish_failure(jdb, job.id, "boom", retry_in=30)
    jdb.refresh(job)
    assert job.status == STATUS_QUEUED and job.error == "boom"
    assert job.next_run_at > utcnow() + timedelta(seconds=25)
    assert engine.claim_next(jdb, "default") is None


def test_final_failure(jdb):
    job = engine.enqueue(jdb, "test.noop")
    engine.claim_next(jdb, "default")
    engine.finish_failure(jdb, job.id, "boom")
    jdb.refresh(job)
    assert job.status == STATUS_FAILED and job.finished_at is not None


def test_cancel_queued_job_is_immediate(jdb):
    job = engine.enqueue(jdb, "test.noop")
    assert engine.request_cancel(jdb, job.id) is True
    jdb.refresh(job)
    assert job.status == STATUS_CANCELLED and job.finished_at is not None


def test_cancel_running_job_only_flags_it(jdb):
    job = engine.enqueue(jdb, "test.noop")
    engine.claim_next(jdb, "default")
    assert engine.request_cancel(jdb, job.id) is True
    jdb.refresh(job)
    assert job.status == STATUS_RUNNING and job.cancel_requested
    assert engine.is_cancel_requested(jdb, job.id)


def test_cancel_finished_or_missing_job_is_noop(jdb):
    job = engine.enqueue(jdb, "test.noop")
    engine.claim_next(jdb, "default")
    engine.finish_success(jdb, job.id)
    assert engine.request_cancel(jdb, job.id) is False
    assert engine.request_cancel(jdb, 99999) is False


def test_manual_retry_resets_job(jdb):
    job = engine.enqueue(jdb, "test.noop", dedupe_key="r")
    engine.claim_next(jdb, "default")
    engine.save_state(jdb, job.id, {"x": 1})
    engine.finish_failure(jdb, job.id, "boom")
    retried = engine.retry(jdb, job.id)
    assert retried.status == STATUS_QUEUED and retried.attempts == 0
    assert retried.error is None and retried.state_json is None
    assert engine.claim_next(jdb, "default").id == job.id


def test_retry_rejects_active_missing_and_duplicate(jdb):
    job = engine.enqueue(jdb, "test.noop", dedupe_key="r")
    with pytest.raises(JobError, match="not_retryable"):
        engine.retry(jdb, job.id)
    with pytest.raises(JobError, match="not_found"):
        engine.retry(jdb, 12345)
    engine.request_cancel(jdb, job.id)
    again = engine.enqueue(jdb, "test.noop", dedupe_key="r")  # key is free: new active job
    with pytest.raises(JobError, match="duplicate"):
        engine.retry(jdb, job.id)
    assert again.id != job.id


def test_recover_requeues_running_jobs_without_consuming_attempt(jdb):
    job = engine.enqueue(jdb, "test.noop")
    engine.claim_next(jdb, "default")
    assert engine.recover_interrupted(jdb) == 1
    jdb.refresh(job)
    assert job.status == STATUS_QUEUED and job.attempts == 0
    assert "Resumed after restart" in _logs(jdb, job.id)
    assert engine.claim_next(jdb, "default").attempts == 1


def test_recover_cancels_jobs_with_pending_cancellation(jdb):
    job = engine.enqueue(jdb, "test.noop")
    engine.claim_next(jdb, "default")
    engine.request_cancel(jdb, job.id)
    engine.recover_interrupted(jdb)
    jdb.refresh(job)
    assert job.status == STATUS_CANCELLED and not job.cancel_requested


def test_recover_ignores_queued_and_finished_jobs(jdb):
    engine.enqueue(jdb, "test.noop")
    done = engine.enqueue(jdb, "test.noop")
    engine.claim_next(jdb, "default")  # takes the first one
    engine.claim_next(jdb, "default")
    engine.finish_success(jdb, done.id)
    assert engine.recover_interrupted(jdb) == 1  # only the still-running first job


def test_purge_old_removes_finished_jobs_and_logs(jdb):
    old = engine.enqueue(jdb, "test.noop")
    fresh = engine.enqueue(jdb, "test.noop")
    running = engine.enqueue(jdb, "test.noop")
    for j in (old, fresh):
        engine.claim_next(jdb, "default")
        engine.finish_success(jdb, j.id)
    jdb.refresh(old)
    old.finished_at = utcnow() - timedelta(days=40)
    running.started_at = utcnow() - timedelta(days=90)
    jdb.commit()
    assert engine.purge_old(jdb, 30) == 1
    assert jdb.get(Job, old.id) is None
    assert jdb.get(Job, fresh.id) is not None and jdb.get(Job, running.id) is not None
    assert _logs(jdb, old.id) == []


def test_list_jobs_filters_and_paginates(jdb):
    for _ in range(5):
        engine.enqueue(jdb, "test.noop")
    engine.enqueue(jdb, "test.other")
    engine.request_cancel(jdb, engine.enqueue(jdb, "test.noop").id)
    jobs, total = engine.list_jobs(jdb, type="test.noop")
    assert total == 6 and jobs[0].id > jobs[-1].id  # newest first
    assert engine.list_jobs(jdb, status=STATUS_CANCELLED)[1] == 1
    page2, total = engine.list_jobs(jdb, per_page=4, page=2)
    assert total == 7 and len(page2) == 3
    assert engine.list_jobs(jdb, status=STATUS_SUCCEEDED) == ([], 0)


def test_backoff_grows_and_is_capped():
    rng = random.Random(1)
    delays = [engine.backoff_delay(n, 30, rng=rng, jitter=0) for n in (1, 2, 3, 4)]
    assert delays == [30, 60, 120, 240]
    assert engine.backoff_delay(20, 30, jitter=0) == engine.BACKOFF_CAP_SECONDS


def test_backoff_jitter_stays_within_ten_percent():
    rng = random.Random(7)
    for _ in range(200):
        d = engine.backoff_delay(2, 30, rng=rng)
        assert 54 <= d <= 66


def test_the_final_logs_are_written_and_trimmed_before_the_status_changes(jdb):
    """Whoever sees the final status must already see the final, trimmed logs."""
    job = engine.enqueue(jdb, "demo", {})
    for i in range(10):
        engine.add_log(jdb, job.id, "info", f"line {i}")
    seen = []

    original = engine.trim_logs

    def spy(db, job_id, max_lines):
        seen.append(db.scalar(select(Job.status).where(Job.id == job_id)))
        return original(db, job_id, max_lines)

    engine.trim_logs = spy
    try:
        engine.finish_success(jdb, job.id, max_lines=5)
    finally:
        engine.trim_logs = original
    assert seen and seen[0] != "succeeded"  # trimmed while the job was not finished yet
    assert len(_logs(jdb, job.id)) == 5 and _logs(jdb, job.id)[-1] == "Completed"
