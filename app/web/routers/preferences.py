"""Personal preferences: language and theme."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, SettingsDep, UserAuth
from app.i18n import LANG_COOKIE, LANG_NAMES, SUPPORTED
from app.models import THEMES
from app.web.templating import current_lang, render

router = APIRouter()


@router.get("/account/preferences")
def preferences_page(request: Request, auth: UserAuth):
    ctx = {
        "languages": LANG_NAMES,
        "current_language": current_lang(request),
        "themes": THEMES,
        "current_theme": auth.user.theme if auth.user.theme in THEMES else "auto",
    }
    return render(request, "account_preferences.html", ctx)


@router.post("/account/preferences")
def preferences_save(
    request: Request,
    db: DB,
    settings: SettingsDep,
    auth: UserAuth,
    language: Annotated[str, Form()] = "",
    theme: Annotated[str, Form()] = "",
):
    if language not in SUPPORTED or theme not in THEMES:
        ctx = {
            "languages": LANG_NAMES,
            "current_language": current_lang(request),
            "themes": THEMES,
            "current_theme": auth.user.theme,
            "error": "error.preferences_invalid",
        }
        return render(request, "account_preferences.html", ctx, status_code=400)
    auth.user.language = language
    auth.user.theme = theme
    db.commit()
    response = RedirectResponse("/account/preferences?flash=preferences_saved", status_code=303)
    response.set_cookie(  # also used by the pages shown before logging in
        LANG_COOKIE,
        language,
        max_age=365 * 24 * 3600,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response
