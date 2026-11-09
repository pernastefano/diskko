from datetime import timedelta
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth.deps import DB, OptionalAuth, SettingsDep, UserAuth
from app.auth.security import dummy_verify, hash_password, needs_rehash, verify_password
from app.auth.sessions import (
    clear_session_cookie,
    create_session,
    revoke_session,
    revoke_user_sessions,
    set_session_cookie,
)
from app.auth.throttle import THROTTLE, client_ip
from app.auth.users import UserError, check_password, has_users
from app.i18n import LANG_COOKIE, SUPPORTED
from app.models import AuditLog, User, utcnow
from app.services import audit
from app.web.templating import render

router = APIRouter()


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow local absolute paths (no open redirect)."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return default
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or "\\" in target:
        return default
    return target


@router.get("/login")
def login_page(request: Request, db: DB, auth: OptionalAuth, next: str = ""):
    if not has_users(db):
        return RedirectResponse("/setup", status_code=303)
    if auth:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next, ""), "username": ""})


@router.post("/login")
def login_submit(
    request: Request,
    db: DB,
    settings: SettingsDep,
    auth: OptionalAuth,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    ip = client_ip(request, settings)
    wait = THROTTLE.retry_after(settings, ip, username)
    if wait:  # paused: no password check, no new failure counted
        name = username.strip().lower()[:32]
        recent = db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == "login.throttled",
                AuditLog.actor_name == name,
                AuditLog.at > utcnow() - timedelta(seconds=settings.login_window_seconds),
            )
        )
        if recent is None:  # one entry per pause: hammering a paused account adds no rows
            audit.record(
                db, None, "login.throttled", actor_name=name, details={"retry_after": wait}
            )
            db.commit()
        ctx = {"next": safe_next(next, ""), "username": username, "error": "error.too_many_logins"}
        response = render(request, "login.html", ctx, status_code=429)
        response.headers["Retry-After"] = str(wait)
        return response
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    error = None
    if user is None:
        dummy_verify(password)
        error = "error.invalid_credentials"
    elif not verify_password(user.password_hash, password):
        error = "error.invalid_credentials"
    elif not user.is_active:
        error = "error.account_disabled"
    if error:
        THROTTLE.failed(ip, username)
        audit.record(
            db,
            user if user is not None and error == "error.account_disabled" else None,
            "login.failed",
            actor_name=username.strip().lower()[:32],
            details={"reason": "disabled" if error == "error.account_disabled" else "credentials"},
        )
        db.commit()
        ctx = {"next": safe_next(next, ""), "username": username, "error": error}
        return render(request, "login.html", ctx, status_code=400)

    THROTTLE.succeeded(username)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    if auth:  # replace any previous session of this browser
        revoke_session(db, auth.session)
    audit.record(db, user, "login.ok")
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(response, create_session(db, user, settings), settings)
    response.delete_cookie("diskko_csrf", path="/")
    return response


@router.post("/logout")
def logout(db: DB, settings: SettingsDep, auth: UserAuth):
    revoke_session(db, auth.session)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, settings)
    return response


def change_password(db, settings, auth, current: str, new: str, confirm: str) -> None:
    """Set the signed-in user's new password (raises UserError); their other sessions end."""
    if not verify_password(auth.user.password_hash, current):
        raise UserError("password_wrong")
    if new != confirm:
        raise UserError("password_mismatch")
    check_password(new, settings)
    auth.user.password_hash = hash_password(new)
    db.commit()
    revoke_user_sessions(db, auth.user.id, except_id=auth.session.id)


@router.get("/account/password")
def password_page(request: Request, auth: UserAuth):
    return render(request, "account_password.html")


@router.post("/account/password")
def password_submit(
    request: Request,
    db: DB,
    settings: SettingsDep,
    auth: UserAuth,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    new_password_confirm: Annotated[str, Form()] = "",
):
    try:
        change_password(db, settings, auth, current_password, new_password, new_password_confirm)
    except UserError as exc:
        return render(request, "account_password.html", {"error": f"error.{exc.code}"}, 400)
    return RedirectResponse("/account/password?flash=password_changed", status_code=303)


@router.post("/lang")
def set_language(
    db: DB,
    settings: SettingsDep,
    auth: OptionalAuth,
    lang: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    response = RedirectResponse(safe_next(next), status_code=303)
    if lang in SUPPORTED:
        response.set_cookie(
            LANG_COOKIE,
            lang,
            max_age=365 * 24 * 3600,
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )
        if auth:
            auth.user.language = lang
            db.commit()
    return response
