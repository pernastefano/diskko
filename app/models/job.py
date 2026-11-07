from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
JOB_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # One active job per dedupe_key; the key is free again once the job is finished.
        Index(
            "uq_jobs_dedupe_active",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
        ),
        Index("ix_jobs_claim", "status", "queue", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    queue: Mapped[str] = mapped_column(String(32), default="default")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_QUEUED)
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    step: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # What the job is about, in words (an artist, an album...): shown and searchable.
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    # NULL means "ready now"; otherwise the earliest time the job may be claimed (retry backoff).
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # Small checkpoint a handler can save to resume after an interruption.
    state_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(Text)
