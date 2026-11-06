from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates

from app.auth.csrf import CSRF_COOKIE, get_csrf_token
from app.i18n import LANG_COOKIE, LANG_NAMES, has_key, resolve_lang, translate
from app.models import utcnow

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") + " UTC" if value else "—"


def duration(job: Any) -> str:
    """Elapsed time of a job as '12s' or '3m 04s'."""
    if not job.started_at:
        return "—"
    seconds = int(((job.finished_at or utcnow()) - job.started_at).total_seconds())
    minutes, secs = divmod(max(seconds, 0), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


from app.services.library_views import describe_format  # noqa: E402

templates.env.filters["fmt_dt"] = fmt_dt
templates.env.filters["album_format"] = describe_format
templates.env.globals["duration"] = duration


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def current_lang(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    return resolve_lang(
        auth.user.language if auth else None,
        request.cookies.get(LANG_COOKIE),
        request.headers.get("accept-language"),
    )


def render(
    request: Request,
    name: str,
    ctx: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    """Render a template with the common context (user, language, t(), CSRF token)."""
    auth = getattr(request.state, "auth", None)
    lang = current_lang(request)
    token, new_cookie = get_csrf_token(request)
    flash_key = f"flash.{request.query_params.get('flash', '')}"
    context: dict[str, Any] = {
        "user": auth.user if auth else None,
        "lang": lang,
        "lang_names": LANG_NAMES,
        "t": partial(translate, lang),
        "csrf_token": token,
        "current_path": request.url.path,
        "min_password_length": request.app.state.settings.min_password_length,
        "flash": flash_key if has_key(flash_key) else None,
    }
    context.update(ctx or {})
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    if new_cookie:
        settings = request.app.state.settings
        response.set_cookie(
            CSRF_COOKIE,
            new_cookie,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )
    return response
