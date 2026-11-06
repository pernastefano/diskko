from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth.deps import LoginRequired
from app.auth.users import has_users
from app.i18n import has_key, translate
from app.web.templating import current_lang, is_htmx, render

_DEFAULT_CODES = {401: "unauthorized", 403: "forbidden", 404: "not_found"}


def _error_code(status: int, detail: object) -> str:
    if isinstance(detail, str) and has_key(f"error.{detail}"):
        return detail
    return _DEFAULT_CODES.get(status, "generic")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        factory = request.app.state.session_factory
        with factory() as db:
            setup_needed = not has_users(db)
        if setup_needed:
            return RedirectResponse("/setup", status_code=303)
        if is_htmx(request):
            return HTMLResponse("", status_code=401, headers={"HX-Redirect": "/login"})
        if request.method == "GET":
            target = request.url.path
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = _error_code(exc.status_code, exc.detail)
        if is_htmx(request):
            message = translate(current_lang(request), f"error.{code}")
            return HTMLResponse(
                f'<div class="text-sm text-red-700">{message}</div>',
                status_code=exc.status_code,
                headers=exc.headers,
            )
        if "text/html" in request.headers.get("accept", ""):
            return render(request, "error.html", {"code": code}, status_code=exc.status_code)
        return await http_exception_handler(request, exc)
