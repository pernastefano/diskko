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
from app.i18n import has_key
from app.models import ROLE_USER, ROLES, User
from app.web.templating import is_htmx, render

router = APIRouter(prefix="/admin/users", dependencies=[Depends(require_admin)])


def _list_page(request: Request, db: DB, ctx: dict | None = None, status_code: int = 200):
    users = db.scalars(select(User).order_by(User.username)).all()
    error_key = f"error.{request.query_params.get('error', '')}"
    context = {
        "users": users,
        "roles": ROLES,
        "form_username": "",
        "form_role": ROLE_USER,
        "error": error_key if has_key(error_key) else None,
    }
    context.update(ctx or {})
    return render(request, "admin/users.html", context, status_code=status_code)


def _get_target(db: DB, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="not_found")
    return target


def _row_result(request: Request, db: DB, target: User, exc: UserError | None = None):
    """HTMX gets the updated row (409 + message on error); plain requests get a redirect."""
    if is_htmx(request):
        if exc:
            db.rollback()
            db.refresh(target)
        ctx = {"u": target, "row_error": f"error.{exc.code}" if exc else None}
        return render(request, "admin/_user_row.html", ctx, status_code=409 if exc else 200)
    suffix = f"error={exc.code}" if exc else "flash=user_updated"
    return RedirectResponse(f"/admin/users?{suffix}", status_code=303)


@router.get("")
def users_list(request: Request, db: DB):
    return _list_page(request, db)


@router.post("")
def users_create(
    request: Request,
    db: DB,
    settings: SettingsDep,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = ROLE_USER,
):
    try:
        create_user(db, username, password, role, settings)
    except UserError as exc:
        ctx = {"error": f"error.{exc.code}", "form_username": username, "form_role": role}
        return _list_page(request, db, ctx, status_code=400)
    return RedirectResponse("/admin/users?flash=user_created", status_code=303)


@router.post("/{user_id}/role")
def users_role(
    request: Request, db: DB, auth: AdminAuth, user_id: int, role: Annotated[str, Form()] = ""
):
    target = _get_target(db, user_id)
    try:
        set_role(db, auth.user, target, role)
    except UserError as exc:
        return _row_result(request, db, target, exc)
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
        return _row_result(request, db, target, exc)
    return _row_result(request, db, target)


@router.post("/{user_id}/delete")
def users_delete(request: Request, db: DB, auth: AdminAuth, user_id: int):
    target = _get_target(db, user_id)
    try:
        delete_user(db, auth.user, target)
    except UserError as exc:
        return _row_result(request, db, target, exc)
    if is_htmx(request):
        return HTMLResponse("")
    return RedirectResponse("/admin/users?flash=user_deleted", status_code=303)
