"""Download requests: what a user asked for and how SLSKD is getting on with it."""

from datetime import datetime

from sqlalchemy import (
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

# DownloadRequest.status
REQ_QUEUED = "queued"
REQ_SEARCHING = "searching"
REQ_CANDIDATE_FOUND = "candidate_found"
REQ_AWAITING_CHOICE = "awaiting_choice"  # manual mode: the user picks one of the candidates
REQ_DOWNLOADING = "downloading"
REQ_DOWNLOADED = "downloaded"  # the files are in the downloads folder, waiting for processing
REQ_POSTPROCESSING = "postprocessing"
REQ_IMPORTING = "importing"
REQ_COMPLETED = "completed"
REQ_FAILED = "failed"
REQ_CANCELLED = "cancelled"
REQ_NO_RESULTS = "no_results"
REQUEST_STATUSES = (
    REQ_QUEUED,
    REQ_SEARCHING,
    REQ_CANDIDATE_FOUND,
    REQ_AWAITING_CHOICE,
    REQ_DOWNLOADING,
    REQ_DOWNLOADED,
    REQ_POSTPROCESSING,
    REQ_IMPORTING,
    REQ_COMPLETED,
    REQ_FAILED,
    REQ_CANCELLED,
    REQ_NO_RESULTS,
)
# A release group has at most one request in these statuses (a partial unique index).
REQ_ACTIVE = (
    REQ_QUEUED,
    REQ_SEARCHING,
    REQ_CANDIDATE_FOUND,
    REQ_AWAITING_CHOICE,
    REQ_DOWNLOADING,
    REQ_DOWNLOADED,
    REQ_POSTPROCESSING,
    REQ_IMPORTING,
)
REQ_FINISHED = (REQ_COMPLETED, REQ_FAILED, REQ_CANCELLED, REQ_NO_RESULTS)

MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODES = (MODE_AUTO, MODE_MANUAL)


class DownloadRequest(Base):
    __tablename__ = "download_requests"
    __table_args__ = (
        Index(
            "uq_download_requests_active_group",
            "release_group_id",
            unique=True,
            sqlite_where=text(
                "status IN ('queued', 'searching', 'candidate_found', 'awaiting_choice', "
                "'downloading', 'downloaded', 'postprocessing', 'importing')"
            ),
        ),
        Index("ix_download_requests_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    release_group_id: Mapped[int] = mapped_column(
        ForeignKey("release_groups.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default=REQ_QUEUED)
    quality_key: Mapped[str] = mapped_column(String(48))  # the minimum quality profile
    mode: Mapped[str] = mapped_column(String(8), default=MODE_AUTO)
    query_override: Mapped[str | None] = mapped_column(String(255), nullable=True)
    query_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    candidate_index: Mapped[int] = mapped_column(Integer, default=-1)  # -1: none chosen yet
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dest_dir: Mapped[str | None] = mapped_column(String(255), nullable=True)  # under downloads
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DownloadFile(Base):
    """One file of the chosen candidate, as SLSKD reports it."""

    __tablename__ = "download_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("download_requests.id", ondelete="CASCADE"), index=True
    )
    transfer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    username: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(Text)  # the remote path, as SLSKD knows it
    subdir: Mapped[str] = mapped_column(String(255), default="")  # disc folder, if any
    size: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(48), default="Requested")
    bytes_transferred: Mapped[int] = mapped_column(Integer, default=0)
    speed: Mapped[float] = mapped_column(Float, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
