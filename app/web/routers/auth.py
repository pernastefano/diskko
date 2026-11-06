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
from app.auth.users import UserError, check_password, has_users
from app.i18n import LANG_COOKIE, SUPPORTED
from app.models import User
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
        ctx = {"next": safe_next(next, ""), "username": username, "error": error}
        return render(request, "login.html", ctx, status_code=400)

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    if auth:  # replace any previous session of this browser
        revoke_session(db, auth.session)
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
        if not verify_password(auth.user.password_hash, current_password):
            raise UserError("password_wrong")
        if new_password != new_password_confirm:
            raise UserError("password_mismatch")
        check_password(new_password, settings)
    except UserError as exc:
        return render(request, "account_password.html", {"error": f"error.{exc.code}"}, 400)
    auth.user.password_hash = hash_password(new_password)
    db.commit()
    revoke_user_sessions(db, auth.user.id, except_id=auth.session.id)
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
