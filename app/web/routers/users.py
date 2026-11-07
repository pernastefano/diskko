from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.auth.deps import DB, AdminAuth, SettingsDep, require_admin
from app.auth.users import (
    UserError,
    create_user,
    delete_user,
    reset_password,
    set_active,
    set_role,
)
from app.models import ROLE_USER, User
from app.services import audit
from app.web.routers.settings import render_users_section
from app.web.templating import is_htmx, render

router = APIRouter(prefix="/admin/users", dependencies=[Depends(require_admin)])


def _get_target(db: DB, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="not_found")
    return target


def _audit_user(db: DB, auth: AdminAuth, action: str, target: User, details=None) -> None:
    audit.record(
        db,
        auth.user,
        action,
        target_type="user",
        target_id=target.id,
        label=target.username,
        details=details,
    )
    db.commit()


def _row_result(
    request: Request, db: DB, target: User, exc: UserError | None = None, *, modal: str = ""
):
    """HTMX gets the updated row (409 + message on error); plain requests get a redirect.

    `modal="reset"` sends a failed password reset back to the page with its dialog open."""
    if is_htmx(request):
        if exc:
            db.rollback()
            db.refresh(target)
        ctx = {"u": target, "row_error": f"error.{exc.code}" if exc else None}
        return render(request, "admin/_user_row.html", ctx, status_code=409 if exc else 200)
    if exc and modal:
        return RedirectResponse(
            f"/settings/users?modal={modal}&uid={target.id}&error={exc.code}", status_code=303
        )
    suffix = f"error={exc.code}" if exc else "flash=user_updated"
    return RedirectResponse(f"/settings/users?{suffix}", status_code=303)


@router.get("")
def users_list():
    """Users are managed in the settings now."""
    return RedirectResponse("/settings/users", status_code=303)


@router.post("")
def users_create(
    request: Request,
    db: DB,
    settings: SettingsDep,
    auth: AdminAuth,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = ROLE_USER,
):
    try:
        create_user(db, username, password, role, settings)
    except UserError as exc:
        return render_users_section(
            request,
            db,
            status_code=400,
            modal="create",
            form={"username": username, "role": role},
            modal_error=f"error.{exc.code}",
        )
    created = db.scalar(select(User).where(User.username == username.strip().lower()))
    audit.record(
        db,
        auth.user,
        "user.created",
        target_type="user",
        target_id=created.id if created else None,
        label=username.strip().lower(),
        details={"role": role},
    )
    db.commit()
    return RedirectResponse("/settings/users?flash=user_created", status_code=303)


@router.post("/{user_id}/role")
def users_role(
    request: Request, db: DB, auth: AdminAuth, user_id: int, role: Annotated[str, Form()] = ""
):
    target = _get_target(db, user_id)
    try:
        set_role(db, auth.user, target, role)
    except UserError as exc:
        return _row_result(request, db, target, exc)
    _audit_user(db, auth, "user.role", target, {"role": role})
    return _row_result(request, db, target)


@router.post("/{user_id}/active")
def users_active(
    request: Request, db: DB, auth: AdminAuth, user_id: int, active: Annotated[str, Form()] = ""
):
    target = _get_target(db, user_id)
    try:
        set_active(db, auth.user, target, active == "1")
    except UserError as exc:
        return _row_result(request, db, target, exc)
    _audit_user(db, auth, "user.active", target, {"active": active == "1"})
    return _row_result(request, db, target)


@router.post("/{user_id}/password")
def users_password(
    request: Request,
    db: DB,
    settings: SettingsDep,
    auth: AdminAuth,
    user_id: int,
    new_password: Annotated[str, Form()] = "",
):
    target = _get_target(db, user_id)
    try:
        reset_password(db, target, new_password, settings)
    except UserError as exc:
        return _row_result(request, db, target, exc, modal="reset")
    _audit_user(db, auth, "user.password_reset", target)  # never the password itself
    return _row_result(request, db, target)


@router.post("/{user_id}/delete")
def users_delete(request: Request, db: DB, auth: AdminAuth, user_id: int):
    target = _get_target(db, user_id)
    name = target.username
    try:
        delete_user(db, auth.user, target)
    except UserError as exc:
        return _row_result(request, db, target, exc)
    audit.record(db, auth.user, "user.deleted", target_type="user", label=name)
    db.commit()
    if is_htmx(request):
        return HTMLResponse("")
    return RedirectResponse("/settings/users?flash=user_deleted", status_code=303)
