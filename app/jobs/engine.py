"""Database operations of the job queue. Pure functions on a SQLAlchemy session.

The worker (app/jobs/worker.py) and the web pages are the only callers. Every function
commits its own short transaction.
"""

import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.jobs import registry
from app.jobs.errors import JobError
from app.jobs.labels import job_label
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
from app.services.app_settings import get_setting, set_setting

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
    label: str | None = None,
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
        label=(label or job_label(db, type, payload or {}) or None),
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


# Jobs that run by themselves all day (service checks, the nightly cleanup): hidden from the list
# unless asked for, and kept for a short time.
ROUTINE_TYPES = (
    "check_integrations",
    "integration_check",
    "housekeeping",
    "queue_pending_covers",
)
SEARCH_MIN = 3  # characters needed to search the logs


def _like(text: str) -> str:
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _job_conditions(type: str | None, search: str | None, routine: bool) -> list:
    """The filters of the job list other than the status."""
    conditions = []
    if type:
        conditions.append(Job.type == type)
    elif not routine:
        conditions.append(Job.type.not_in(ROUTINE_TYPES))
    text = (search or "").strip()
    if len(text) >= SEARCH_MIN:
        like = _like(text)
        in_logs = select(JobLog.job_id).where(func.lower(JobLog.message).like(like, escape="\\"))
        conditions.append(
            or_(
                func.lower(Job.type).like(like, escape="\\"),
                func.lower(func.coalesce(Job.label, "")).like(like, escape="\\"),
                Job.id.in_(in_logs),
            )
        )
    return conditions


def list_jobs(
    db: Session,
    *,
    type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    routine: bool = True,
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[Job], int]:
    """A page of jobs, newest first. `search` looks in the type, the label and every log line;
    `routine=False` hides the jobs that run all day (unless a type is asked for)."""
    conditions = _job_conditions(type, search, routine)
    if status:
        conditions.append(Job.status == status)
    total = db.scalar(select(func.count(Job.id)).where(*conditions)) or 0
    page = max(page, 1)
    rows = db.scalars(
        select(Job)
        .where(*conditions)
        .order_by(Job.id.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()
    return list(rows), total


def status_counts(
    db: Session, *, type: str | None = None, search: str | None = None, routine: bool = True
) -> dict[str, int]:
    """How many jobs each status has under the other filters (the numbers of the status buttons)."""
    rows = db.execute(
        select(Job.status, func.count(Job.id))
        .where(*_job_conditions(type, search, routine))
        .group_by(Job.status)
    )
    return dict(rows.all())


def matching_lines(
    db: Session, job_ids: list[int], search: str, *, per_job: int = 3
) -> dict[int, list[str]]:
    """For each job, the first log lines that contain the searched text."""
    text = (search or "").strip()
    if len(text) < SEARCH_MIN or not job_ids:
        return {}
    rows = db.execute(
        select(JobLog.job_id, JobLog.message)
        .where(
            JobLog.job_id.in_(job_ids), func.lower(JobLog.message).like(_like(text), escape="\\")
        )
        .order_by(JobLog.id)
    )
    found: dict[int, list[str]] = {}
    for job_id, message in rows:
        lines = found.setdefault(job_id, [])
        if len(lines) < per_job:
            lines.append(message)
    return found


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


DEFER_LIMIT = timedelta(hours=24)  # how long a job may keep waiting for a service


def waited_too_long(db: Session, job_id: int, *, now: Callable[[], datetime] = utcnow) -> bool:
    """Has the job been waiting for a service (put back without using an attempt) for so long
    that it is time to stop? A service that is dead for good must not keep a job for ever."""
    created = db.scalar(select(Job.created_at).where(Job.id == job_id))
    return created is not None and now() - created > DEFER_LIMIT


def requeue_later(db: Session, job_id: int, message: str, delay: float) -> None:
    """Put a running job back in the queue after `delay` seconds without using an attempt.
    After DEFER_LIMIT of waiting the job fails instead (see `waited_too_long`)."""
    if waited_too_long(db, job_id):
        finish_failure(
            db, job_id, f"Gave up after {DEFER_LIMIT.total_seconds() / 3600:.0f}h: {message}"
        )
        return
    db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == STATUS_RUNNING)
        .values(
            status=STATUS_QUEUED,
            attempts=func.max(Job.attempts - 1, 0),
            next_run_at=utcnow() + timedelta(seconds=delay),
        )
    )
    db.commit()
    add_log(db, job_id, "warning", f"{message}. Trying again in {delay:.0f}s (attempt not used)")


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


@dataclass(frozen=True)
class RetentionPolicy:
    """How many days finished jobs (and their logs) are kept; at least 1 each."""

    routine_days: int = 1  # the jobs that run all day, whatever their outcome
    success_days: int = 14  # everything else that succeeded or was cancelled
    failed_days: int = 60  # everything else that failed
    overrides: dict[str, int] = field(default_factory=dict)  # per job type, wins over the rest


def purge_jobs(
    db: Session, policy: RetentionPolicy, *, now: Callable[[], datetime] = utcnow
) -> int:
    """Delete the finished jobs that are older than the policy allows; the logs go with them
    (FK cascade). Returns how many jobs were removed."""
    moment = now()
    finished = and_(Job.status.in_(TERMINAL_STATUSES), Job.finished_at.is_not(None))
    old = lambda days: Job.finished_at < moment - timedelta(days=max(days, 1))  # noqa: E731
    overridden = list(policy.overrides)
    rules = [and_(Job.type == job_type, old(days)) for job_type, days in policy.overrides.items()]
    rest = [Job.type.not_in(overridden)] if overridden else []
    rules.append(and_(Job.type.in_(ROUTINE_TYPES), *rest, old(policy.routine_days)))
    ordinary = [Job.type.not_in(ROUTINE_TYPES), *rest]
    rules.append(and_(*ordinary, Job.status == STATUS_FAILED, old(policy.failed_days)))
    rules.append(and_(*ordinary, Job.status != STATUS_FAILED, old(policy.success_days)))
    result = db.execute(delete(Job).where(finished, or_(*rules)))
    db.commit()
    return result.rowcount


def compact_database(session_factory) -> None:
    """Give the space of deleted rows back to the disk (SQLite keeps it otherwise)."""
    engine_ = session_factory.kw["bind"]
    with engine_.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.exec_driver_sql("VACUUM")


FAILURE_WINDOW = timedelta(hours=24)  # how long a failure keeps the Jobs icon red


@dataclass(frozen=True)
class JobsHealth:
    active: int  # running, or queued and ready to run now (not waiting for a retry)
    failed: int  # failed, not seen yet, and not fixed by a later success of the same job type


def jobs_health(
    db: Session,
    *,
    now: Callable[[], datetime] = utcnow,
    window: timedelta = FAILURE_WINDOW,
    seen_at: datetime | None = None,
) -> JobsHealth:
    """What the Jobs icon shows: yellow while something is running (or about to), red for
    failures the user has not looked at. A failure stops counting after `window`, once the user
    has seen the job list after it (`seen_at`), or as soon as a job of the same type succeeded
    after it (the problem is evidently gone). A job waiting for its retry is not "in progress"."""
    current = now()
    cutoff = current - window
    active = (
        db.scalar(
            select(func.count(Job.id)).where(
                or_(
                    Job.status == STATUS_RUNNING,
                    and_(
                        Job.status == STATUS_QUEUED,
                        or_(Job.next_run_at.is_(None), Job.next_run_at <= current),
                    ),
                )
            )
        )
        or 0
    )
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
                Job.status == STATUS_FAILED,
                Job.finished_at >= (max(cutoff, seen_at) if seen_at else cutoff),
                ~fixed,
            )
        )
        or 0
    )
    return JobsHealth(active, failed)


SEEN_KEY = "jobs.failures_seen"  # per user: when they last looked at the job list


def failures_seen_at(db: Session, user_id: int) -> datetime | None:
    raw = get_setting(db, SEEN_KEY, None, user_id=user_id)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def mark_failures_seen(db: Session, user_id: int, *, now: Callable[[], datetime] = utcnow) -> None:
    """The user has looked at the job list: the failures so far stop colouring the icon. The
    caller commits."""
    set_setting(db, SEEN_KEY, now().isoformat(), user_id=user_id)


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
