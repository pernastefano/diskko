"""The audit log (who did what) and the in-app notifications."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

# Notification.kind
NOTE_COMPLETED = "completed"
NOTE_FAILED = "failed"
NOTE_NO_RESULTS = "no_results"
NOTE_NEEDS_CHOICE = "needs_choice"
NOTE_KINDS = (NOTE_COMPLETED, NOTE_FAILED, NOTE_NO_RESULTS, NOTE_NEEDS_CHOICE)


class AuditLog(Base):
    """One thing somebody did. The name is copied: the entry outlives the user."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_actor_at", "actor_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(48), index=True)
    target_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Notification(Base):
    """Something that happened to a user's request, to be seen in the app."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("request_id", "kind"),
        Index("ix_notifications_user_read", "user_id", "read_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_requests.id", ondelete="CASCADE"), nullable=True
    )
    album_label: Mapped[str] = mapped_column(String(255), default="")  # "Artist – Album"
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
