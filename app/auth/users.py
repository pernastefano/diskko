"""User management rules shared by setup, admin CRUD and password change."""

import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.auth.sessions import revoke_user_sessions
from app.config import Settings
from app.models import ROLE_ADMIN, ROLES, User

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


class UserError(Exception):
    """A rule violation; `code` maps to the i18n key `error.<code>`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_username(raw: str) -> str:
    name = raw.strip()
    if not USERNAME_RE.match(name):
        raise UserError("username_invalid")
    return name.lower()


def check_password(password: str, settings: Settings) -> None:
    if len(password) < settings.min_password_length:
        raise UserError("password_short")


def has_users(db: Session) -> bool:
    return db.scalar(select(func.count(User.id))) > 0


def count_active_admins(db: Session) -> int:
    return db.scalar(
        select(func.count(User.id)).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
    )


def create_user(
    db: Session,
    username: str,
    password: str,
    role: str,
    settings: Settings,
    language: str | None = None,
) -> User:
    name = normalize_username(username)
    check_password(password, settings)
    if role not in ROLES:
        raise UserError("role_invalid")
    if db.scalar(select(User.id).where(User.username == name)) is not None:
        raise UserError("username_taken")
    user = User(
        username=name,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        language=language,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:  # concurrent insert of the same username
        db.rollback()
        raise UserError("username_taken") from exc
    return user


def _guard_admin_change(db: Session, actor: User, target: User) -> None:
    if actor.id == target.id:
        raise UserError("self_action")
    if target.is_admin and target.is_active and count_active_admins(db) <= 1:
        raise UserError("last_admin")


def set_role(db: Session, actor: User, target: User, role: str) -> None:
    if role not in ROLES:
        raise UserError("role_invalid")
    if role != target.role:
        _guard_admin_change(db, actor, target)
    target.role = role
    db.commit()


def set_active(db: Session, actor: User, target: User, active: bool) -> None:
    if not active:
        _guard_admin_change(db, actor, target)
    target.is_active = active
    db.commit()
    if not active:
        revoke_user_sessions(db, target.id)


def reset_password(db: Session, target: User, new_password: str, settings: Settings) -> None:
    check_password(new_password, settings)
    target.password_hash = hash_password(new_password)
    db.commit()
    revoke_user_sessions(db, target.id)


def delete_user(db: Session, actor: User, target: User) -> None:
    _guard_admin_change(db, actor, target)
    db.delete(target)
    db.commit()
