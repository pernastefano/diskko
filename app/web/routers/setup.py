from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, SettingsDep
from app.auth.sessions import create_session, set_session_cookie
from app.auth.users import UserError, create_user, has_users
from app.models import ROLE_ADMIN
from app.web.templating import current_lang, render

router = APIRouter()


@router.get("/setup")
def setup_page(request: Request, db: DB):
    if has_users(db):
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html", {"username": ""})


@router.post("/setup")
def setup_submit(
    request: Request,
    db: DB,
    settings: SettingsDep,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
):
    if has_users(db):
        raise HTTPException(status_code=404, detail="not_found")
    error = None
    try:
        if password != password_confirm:
            raise UserError("password_mismatch")
        user = create_user(
            db, username, password, ROLE_ADMIN, settings, language=current_lang(request)
        )
    except UserError as exc:
        error = f"error.{exc.code}"
    if error:
        return render(request, "setup.html", {"username": username, "error": error}, 400)
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, create_session(db, user, settings), settings)
    response.delete_cookie("diskko_csrf", path="/")
    return response
