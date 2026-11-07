"""Personal preferences: language, theme and the quality of one's own requests."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, SettingsDep, UserAuth
from app.i18n import LANG_COOKIE, LANG_NAMES, SUPPORTED
from app.models import THEMES
from app.services.app_settings import get_setting, set_setting
from app.services.downloads import USER_QUALITY_KEY
from app.services.quality_profiles import default_key, load_profiles
from app.web.templating import current_lang, render

router = APIRouter()


def _context(request: Request, db: DB, auth: UserAuth) -> dict:
    profiles = load_profiles(db)
    chosen = get_setting(db, USER_QUALITY_KEY, "", auth.user.id)
    return {
        "languages": LANG_NAMES,
        "current_language": current_lang(request),
        "themes": THEMES,
        "current_theme": auth.user.theme if auth.user.theme in THEMES else "auto",
        # best first, like the settings
        "profiles": [(p.key, p.name) for p in reversed(profiles.profiles)],
        "current_quality": chosen if profiles.get(chosen) else "",
        "system_quality": profiles.name(default_key(db)),
    }


@router.get("/account/preferences")
def preferences_page(request: Request, db: DB, auth: UserAuth):
    return render(request, "account_preferences.html", _context(request, db, auth))


@router.post("/account/preferences")
def preferences_save(
    request: Request,
    db: DB,
    settings: SettingsDep,
    auth: UserAuth,
    language: Annotated[str, Form()] = "",
    theme: Annotated[str, Form()] = "",
    quality: Annotated[str, Form()] = "",
):
    profiles = load_profiles(db)
    if language not in SUPPORTED or theme not in THEMES or (quality and not profiles.get(quality)):
        ctx = _context(request, db, auth) | {"error": "error.preferences_invalid"}
        return render(request, "account_preferences.html", ctx, status_code=400)
    auth.user.language = language
    auth.user.theme = theme
    set_setting(db, USER_QUALITY_KEY, quality, auth.user.id)  # "" means the system's quality
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
