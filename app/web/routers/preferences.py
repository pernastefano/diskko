"""Which kinds of missing albums to show: per user, falling back to the shared default."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, AdminAuth, UserAuth
from app.services.musicbrainz.categories import CATEGORIES
from app.services.musicbrainz.missing import (
    clear_user_missing_types,
    get_missing_types,
    has_user_override,
    set_missing_types,
)
from app.web.templating import render

router = APIRouter()


@router.get("/account/preferences")
def preferences_page(request: Request, db: DB, auth: UserAuth):
    ctx = {
        "categories": CATEGORIES,
        "selected": get_missing_types(db, auth.user.id),
        "custom": has_user_override(db, auth.user.id),
        "shared": get_missing_types(db),
    }
    return render(request, "account_preferences.html", ctx)


@router.post("/account/preferences")
def preferences_save(
    db: DB,
    auth: UserAuth,
    mode: Annotated[str, Form()] = "default",
    types: Annotated[list[str] | None, Form()] = None,
):
    if mode == "custom":
        set_missing_types(db, types or [], user_id=auth.user.id)
    else:
        clear_user_missing_types(db, auth.user.id)
    db.commit()
    return RedirectResponse("/account/preferences?flash=preferences_saved", status_code=303)


@router.post("/admin/library/missing-types")
def shared_types_save(db: DB, auth: AdminAuth, types: Annotated[list[str] | None, Form()] = None):
    set_missing_types(db, types or [])
    db.commit()
    return RedirectResponse("/admin/library?flash=missing_types_saved", status_code=303)
