"""In-app notifications: what happened to a user's requests, shown by the bell in the header."""

from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models import (
    NOTE_COMPLETED,
    NOTE_FAILED,
    NOTE_NEEDS_CHOICE,
    NOTE_NO_RESULTS,
    Artist,
    DownloadRequest,
    Notification,
    ReleaseGroup,
    utcnow,
)

READ_KEPT_DAYS = 30
LIST_SIZE = 50

# The request status that tells the user something.
KIND_OF_STATUS = {
    "completed": NOTE_COMPLETED,
    "failed": NOTE_FAILED,
    "no_results": NOTE_NO_RESULTS,
    "awaiting_choice": NOTE_NEEDS_CHOICE,
}


def notify_request(db: Session, request: DownloadRequest, status: str) -> Notification | None:
    """Tell the owner of a request about its new status, once per kind. The caller commits.
    Statuses that need no news (searching, downloading...) do nothing."""
    kind = KIND_OF_STATUS.get(status)
    if kind is None:
        return None
    exists = db.scalar(
        select(Notification.id).where(
            Notification.request_id == request.id, Notification.kind == kind
        )
    )
    if exists:
        return None
    group = db.get(ReleaseGroup, request.release_group_id)
    artist = db.get(Artist, group.artist_id) if group else None
    label = f"{artist.name} – {group.title}" if artist and group else ""
    note = Notification(
        user_id=request.user_id,
        kind=kind,
        request_id=request.id,
        album_label=label[:255],
        detail=(request.error or None) and request.error[:255],
    )
    db.add(note)
    db.flush()
    return note


def unread_count(db: Session, user_id: int) -> int:
    return (
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id, Notification.read_at.is_(None)
            )
        )
        or 0
    )


def list_for(db: Session, user_id: int, limit: int = LIST_SIZE) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.id.desc())
            .limit(limit)
        )
    )


def mark_read(db: Session, user_id: int, note_id: int | None = None) -> int:
    """Mark one notification (or, with no id, all) of a user as read. Never another user's.
    The caller commits. Returns how many changed."""
    stmt = (
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    if note_id is not None:
        stmt = stmt.where(Notification.id == note_id)
    return db.execute(stmt).rowcount


def purge(db: Session, days: int = READ_KEPT_DAYS) -> int:
    """Delete the read notifications older than `days`. The caller commits."""
    cutoff = utcnow() - timedelta(days=days)
    return db.execute(
        delete(Notification).where(Notification.read_at.is_not(None), Notification.read_at < cutoff)
    ).rowcount
