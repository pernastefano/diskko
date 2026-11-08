"""The audit log: who did what, and when.

Entries are written next to the action (the caller commits, so an entry exists exactly when the
action does). They name the actor and the thing acted on, with a few plain details: never a
password, a token or the value of a secret setting.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, User, utcnow

# Every action that is written, for the filter and the translations (audit.action.<name>).
ACTIONS = (
    "login.ok",
    "login.failed",
    "request.created",
    "request.cancelled",
    "request.retried",
    "request.chosen",
    "request.deleted",
    "request.upgrade",
    "user.created",
    "user.role",
    "user.active",
    "user.password_reset",
    "user.deleted",
    "settings.saved",
    "quality.changed",
    "artist.sync",
    "artist.resolved",
    "artist.dismissed",
    "artist.reset",
    "artist.confirmed",
    "album.match",
    "album.refresh",
    "library.scan",
)
PAGE_SIZE = 50
_SECRET_WORDS = ("password", "token", "secret", "key", "passwd")
_MAX_LABEL = 255


def _plain(details: dict[str, Any] | None) -> str | None:
    """Details as JSON, without anything that looks like a secret."""
    if not details:
        return None
    clean = {
        k: v
        for k, v in details.items()
        if not any(word in k.lower() for word in _SECRET_WORDS)
        and isinstance(v, str | int | float | bool | list | type(None))
    }
    return json.dumps(clean, ensure_ascii=False, default=str)[:2000] if clean else None


def record(
    db: Session,
    actor: User | None,
    action: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    label: str | None = None,
    details: dict[str, Any] | None = None,
    actor_name: str | None = None,
) -> AuditLog:
    """Add an entry (the caller commits). `actor_name` is for the ones with no user yet: a failed
    login names the name that was tried."""
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        actor_name=(actor.username if actor else (actor_name or ""))[:64],
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_label=label[:_MAX_LABEL] if label else None,
        details_json=_plain(details),
    )
    db.add(entry)
    db.flush()
    return entry


@dataclass
class AuditPage:
    entries: list[AuditLog]
    total: int
    page: int
    pages: int


def list_entries(
    db: Session,
    *,
    actor: str = "",
    action: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    size: int = PAGE_SIZE,
) -> AuditPage:
    stmt = select(AuditLog)
    if actor:
        stmt = stmt.where(AuditLog.actor_name == actor.strip().lower())
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if since:
        stmt = stmt.where(AuditLog.at >= since)
    if until:
        stmt = stmt.where(AuditLog.at < until + timedelta(days=1))  # the whole last day
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max((total + size - 1) // size, 1)
    page = min(max(page, 1), pages)
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size)).all()
    return AuditPage(list(rows), total, page, pages)


def known_actors(db: Session) -> list[str]:
    names = db.scalars(select(AuditLog.actor_name).where(AuditLog.actor_name != "").distinct())
    return sorted(names)


def purge(db: Session, days: int) -> int:
    """Delete the entries older than `days`. The caller commits."""
    cutoff = utcnow() - timedelta(days=max(days, 1))
    return db.execute(delete(AuditLog).where(AuditLog.at < cutoff)).rowcount
