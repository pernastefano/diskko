import hashlib
import secrets
from datetime import timedelta

from fastapi import Response
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User, UserSession, utcnow

SLIDE_AFTER = timedelta(minutes=5)


def _serializer(settings: Settings) -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt="diskko-session")


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_session(db: Session, user: User, settings: Settings) -> str:
    """Create a server-side session and return the signed cookie value."""
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(
        UserSession(
            token_hash=_hash(raw),
            user_id=user.id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
        )
    )
    db.commit()
    return _serializer(settings).dumps(raw)


def load_session(
    db: Session, cookie_value: str, settings: Settings
) -> tuple[UserSession | None, bool]:
    """Return (session, refreshed). Invalid, expired or inactive-user sessions give None."""
    try:
        raw = _serializer(settings).loads(cookie_value)
    except BadSignature:
        return None, False
    row = db.scalar(select(UserSession).where(UserSession.token_hash == _hash(str(raw))))
    if row is None:
        return None, False
    now = utcnow()
    if row.expires_at <= now:
        db.delete(row)
        db.commit()
        return None, False
    if not row.user.is_active:
        return None, False
    refreshed = False
    if now - row.last_seen > SLIDE_AFTER:
        row.last_seen = now
        row.expires_at = now + timedelta(hours=settings.session_ttl_hours)
        db.commit()
        refreshed = True
    return row, refreshed


def revoke_session(db: Session, session: UserSession) -> None:
    db.delete(session)
    db.commit()


def revoke_user_sessions(db: Session, user_id: int, except_id: int | None = None) -> None:
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if except_id is not None:
        stmt = stmt.where(UserSession.id != except_id)
    db.execute(stmt)
    db.commit()


def set_session_cookie(response: Response, value: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        value,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
