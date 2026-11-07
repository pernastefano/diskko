"""Database operations of the job queue. Pure functions on a SQLAlchemy session.

The worker (app/jobs/worker.py) and the web pages are the only callers. Every function
commits its own short transaction.
"""

import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.jobs import registry
from app.jobs.errors import JobError
from app.models import (
    ACTIVE_STATUSES,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    Job,
    JobLog,
    utcnow,
)

BACKOFF_CAP_SECONDS = 15 * 60
BACKOFF_JITTER = 0.10


def backoff_delay(
    attempts: int,
    base: float,
    *,
    cap: float = BACKOFF_CAP_SECONDS,
    jitter: float = BACKOFF_JITTER,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff (base, 2*base, 4*base, ...) capped, with +/- jitter."""
    delay = min(cap, base * (2 ** max(attempts - 1, 0)))
    r = (rng or random).uniform(-jitter, jitter)
    return max(0.0, delay * (1 + r))


# ---------------------------------------------------------------- enqueue / read


def enqueue(
    db: Session,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    created_by: int | None = None,
    parent_job_id: int | None = None,
    delay: float | None = None,
) -> Job:
    """Queue a job. With a dedupe_key, an already active job with that key is returned."""
    spec = registry.get_spec(type)
    if spec is None:
        raise JobError(f"unknown job type: {type}")
    if dedupe_key:
        existing = _active_by_key(db, dedupe_key)
        if existing:
            return existing
    now = utcnow()
    job = Job(
        type=type,
        queue=spec.queue,
        payload_json=json.dumps(payload or {}),
        status=STATUS_QUEUED,
        created_by=created_by,
        created_at=now,
        parent_job_id=parent_job_id,
        dedupe_key=dedupe_key,
        max_attempts=spec.max_attempts,
        next_run_at=now + timedelta(seconds=delay) if delay else None,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:  # lost a race on the dedupe key
        db.rollback()
        existing = _active_by_key(db, dedupe_key) if dedupe_key else None
        if existing is None:
            raise
        return existing
    add_log(db, job.id, "info", "Queued")
    return job


def _active_by_key(db: Session, key: str) -> Job | None:
    return db.scalar(select(Job).where(Job.dedupe_key == key, Job.status.in_(ACTIVE_STATUSES)))


def get_job(db: Session, job_id: int) -> Job | None:
    return db.get(Job, job_id)


def list_jobs(
    db: Session,
    *,
    type: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[Job], int]:
    stmt = select(Job)
    count_stmt = select(func.count(Job.id))
    if type:
        stmt = stmt.where(Job.type == type)
        count_stmt = count_stmt.where(Job.type == type)
    if status:
        stmt = stmt.where(Job.status == status)
        count_stmt = count_stmt.where(Job.status == status)
    total = db.scalar(count_stmt) or 0
    page = max(page, 1)
    rows = db.scalars(
        stmt.order_by(Job.id.desc()).limit(per_page).offset((page - 1) * per_page)
    ).all()
    return list(rows), total


# ------------------------------------------------------------------- claiming


def claim_next(db: Session, queue: str, now: datetime | None = None) -> Job | None:
    """Atomically move the oldest ready job of `queue` to running and return it."""
    now = now or utcnow()
    candidate = db.scalar(
        select(Job.id)
        .where(
            Job.status == STATUS_QUEUED,
            Job.queue == queue,
            or_(Job.next_run_at.is_(None), Job.next_run_at <= now),
        )
        .order_by(func.coalesce(Job.next_run_at, Job.created_at), Job.id)
        .limit(1)
    )
    if candidate is None:
        return None
    result = db.execute(
        update(Job)
        .where(Job.id == candidate, Job.status == STATUS_QUEUED)
        .values(
            status=STATUS_RUNNING,
            started_at=now,
            attempts=Job.attempts + 1,
            next_run_at=None,
            finished_at=None,
        )
    )
    db.commit()
    if result.rowcount != 1:  # another worker took it
        return None
    job = db.get(Job, candidate)
    db.refresh(job)
    add_log(db, job.id, "info", f"Started (attempt {job.attempts}/{job.max_attempts})")
    return job


# ------------------------------------------------------------ progress and logs


def update_progress(db: Session, job_id: int, pct: float, step: str | None = None) -> None:
    values: dict[str, Any] = {"progress_pct": max(0.0, min(100.0, float(pct)))}
    if step is not None:
        values["step"] = step[:255]
    db.execute(update(Job).where(Job.id == job_id).values(**values))
    db.commit()


def add_log(db: Session, job_id: int, level: str, message: str) -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message, ts=utcnow()))
    db.commit()


def logs_after(db: Session, job_id: int, after: int = 0, limit: int = 500) -> list[JobLog]:
    return list(
        db.scalars(
            select(JobLog)
            .where(JobLog.job_id == job_id, JobLog.id > after)
            .order_by(JobLog.id)
            .limit(limit)
        )
    )


def trim_logs(db: Session, job_id: int, max_lines: int) -> int:
    """Keep only the newest `max_lines` log lines of a job."""
    keep_from = db.scalar(
        select(JobLog.id)
        .where(JobLog.job_id == job_id)
        .order_by(JobLog.id.desc())
        .offset(max_lines - 1)
        .limit(1)
    )
    if keep_from is None:
        return 0
    result = db.execute(delete(JobLog).where(JobLog.job_id == job_id, JobLog.id < keep_from))
    db.commit()
    return result.rowcount


def get_state(db: Session, job_id: int) -> dict[str, Any]:
    raw = db.scalar(select(Job.state_json).where(Job.id == job_id))
    return json.loads(raw) if raw else {}


def save_state(db: Session, job_id: int, state: dict[str, Any]) -> None:
    db.execute(update(Job).where(Job.id == job_id).values(state_json=json.dumps(state)))
    db.commit()


# ------------------------------------------------------------------- outcomes


def _close_logs(db: Session, job_id: int, level: str, message: str, max_lines: int | None) -> None:
    """Write the last log line and trim BEFORE the status changes, so whoever sees the final
    status also sees the final logs."""
    add_log(db, job_id, level, message)
    if max_lines:
        trim_logs(db, job_id, max_lines)


def finish_success(db: Session, job_id: int, *, max_lines: int | None = None) -> None:
    _close_logs(db, job_id, "info", "Completed", max_lines)
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status=STATUS_SUCCEEDED, progress_pct=100.0, finished_at=utcnow(), error=None)
    )
    db.commit()


def finish_failure(
    db: Session,
    job_id: int,
    error: str,
    *,
    retry_in: float | None = None,
    max_lines: int | None = None,
) -> None:
    """Fail the job for good, or (with retry_in) put it back in the queue after a delay."""
    if retry_in is None:
        _close_logs(db, job_id, "error", f"Failed: {error}", max_lines)
        db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status=STATUS_FAILED, finished_at=utcnow(), error=error)
        )
        db.commit()
    else:
        _close_logs(
            db,
            job_id,
            "warning",
            f"Attempt failed: {error}. Retrying in {retry_in:.0f}s",
            max_lines,
        )
        db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=STATUS_QUEUED,
                error=error,
                next_run_at=utcnow() + timedelta(seconds=retry_in),
            )
        )
        db.commit()


def finish_cancelled(db: Session, job_id: int, message: str = "Cancelled") -> None:
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status=STATUS_CANCELLED, finished_at=utcnow(), cancel_requested=False)
    )
    db.commit()
    add_log(db, job_id, "warning", message)


def requeue_interrupted(db: Session, job_id: int, message: str) -> None:
    """Put a running job back in the queue without consuming an attempt."""
    db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == STATUS_RUNNING)
        .values(
            status=STATUS_QUEUED,
            attempts=func.max(Job.attempts - 1, 0),
            next_run_at=None,
        )
    )
    db.commit()
    add_log(db, job_id, "warning", message)


# --------------------------------------------------------- user-driven actions


def request_cancel(db: Session, job_id: int) -> bool:
    """Cancel a queued job right away, or flag a running one (cooperative cancellation)."""
    job = db.get(Job, job_id)
    if job is None or job.status not in ACTIVE_STATUSES:
        return False
    if job.status == STATUS_QUEUED:
        finish_cancelled(db, job_id, "Cancelled before start")
        return True
    db.execute(update(Job).where(Job.id == job_id).values(cancel_requested=True))
    db.commit()
    add_log(db, job_id, "warning", "Cancellation requested")
    return True


def is_cancel_requested(db: Session, job_id: int) -> bool:
    return bool(db.scalar(select(Job.cancel_requested).where(Job.id == job_id)))


def retry(db: Session, job_id: int) -> Job:
    """Manually re-queue a failed or cancelled job with a fresh attempt budget."""
    job = db.get(Job, job_id)
    if job is None:
        raise JobError("not_found")
    if job.status not in (STATUS_FAILED, STATUS_CANCELLED):
        raise JobError("not_retryable")
    if job.dedupe_key and _active_by_key(db, job.dedupe_key):
        raise JobError("duplicate")
    job.status = STATUS_QUEUED
    job.attempts = 0
    job.error = None
    job.progress_pct = 0.0
    job.step = None
    job.cancel_requested = False
    job.next_run_at = None
    job.started_at = None
    job.finished_at = None
    job.state_json = None
    db.commit()
    add_log(db, job.id, "info", "Re-queued manually")
    return job


# ---------------------------------------------------------- recovery and cleanup


def recover_interrupted(db: Session) -> int:
    """At startup: running jobs were interrupted by a crash or restart. Re-queue them."""
    jobs = db.scalars(select(Job).where(Job.status == STATUS_RUNNING)).all()
    for job in jobs:
        if job.cancel_requested:
            finish_cancelled(db, job.id, "Cancelled (cancellation was pending at restart)")
        else:
            requeue_interrupted(db, job.id, "Resumed after restart")
    return len(jobs)


def purge_old(db: Session, days: int) -> int:
    """Delete finished jobs (and, by FK cascade, their logs) older than `days`."""
    cutoff = utcnow() - timedelta(days=days)
    result = db.execute(
        delete(Job).where(
            and_(Job.status.in_(TERMINAL_STATUSES), Job.finished_at.is_not(None)),
            Job.finished_at < cutoff,
        )
    )
    db.commit()
    return result.rowcount


FAILURE_WINDOW = timedelta(hours=24)  # how long a failure keeps the Jobs icon red


@dataclass(frozen=True)
class JobsHealth:
    active: int  # queued or running
    failed: int  # failed recently and not fixed by a later success of the same job type


def jobs_health(
    db: Session, *, now: Callable[[], datetime] = utcnow, window: timedelta = FAILURE_WINDOW
) -> JobsHealth:
    """What the Jobs icon shows. A failure stops counting after `window`, or as soon as a
    job of the same type succeeded after it (the problem is evidently gone)."""
    active = db.scalar(select(func.count(Job.id)).where(Job.status.in_(ACTIVE_STATUSES))) or 0
    later = aliased(Job)
    fixed = (
        select(later.id)
        .where(
            later.type == Job.type,
            later.status == STATUS_SUCCEEDED,
            later.finished_at > Job.finished_at,
        )
        .exists()
    )
    failed = (
        db.scalar(
            select(func.count(Job.id)).where(
                Job.status == STATUS_FAILED, Job.finished_at >= now() - window, ~fixed
            )
        )
        or 0
    )
    return JobsHealth(active, failed)


def queue_stats(db: Session) -> dict[str, dict[str, int]]:
    """{queue: {'queued': n, 'running': n}} for the admin status page."""
    rows = db.execute(
        select(Job.queue, Job.status, func.count(Job.id))
        .where(Job.status.in_(ACTIVE_STATUSES))
        .group_by(Job.queue, Job.status)
    )
    stats: dict[str, dict[str, int]] = {}
    for queue, status, count in rows:
        stats.setdefault(queue, {"queued": 0, "running": 0})[status] = count
    return stats
