import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from app.auth.deps import DB, resolve_auth
from app.config import Settings

CSRF_COOKIE = "diskko_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _serializer(settings: Settings) -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt="diskko-csrf")


def anon_cookie_value(settings: Settings, token: str) -> str:
    return _serializer(settings).dumps(token)


def _anon_token(request: Request) -> str | None:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie:
        return None
    try:
        return str(_serializer(request.app.state.settings).loads(cookie))
    except BadSignature:
        return None


def get_csrf_token(request: Request) -> tuple[str, str | None]:
    """Return (token, new_cookie_value). new_cookie_value is set only for new anon tokens.

    Logged-in users use the token stored in their session; anonymous visitors (login,
    setup) use a signed double-submit cookie.
    """
    auth = getattr(request.state, "auth", None)
    if auth is not None:
        return auth.session.csrf_token, None
    existing = _anon_token(request)
    if existing:
        return existing, None
    token = secrets.token_urlsafe(32)
    return token, anon_cookie_value(request.app.state.settings, token)


async def csrf_protect(request: Request, db: DB) -> None:
    """App-wide dependency: every unsafe request must carry a valid CSRF token."""
    if request.method in SAFE_METHODS:
        return
    auth = resolve_auth(request, db)
    expected = auth.session.csrf_token if auth else _anon_token(request)
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        form = await request.form()
        value = form.get("csrf_token")
        supplied = value if isinstance(value, str) else None
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="csrf")
