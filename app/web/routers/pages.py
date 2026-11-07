from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import AdminAuth, UserAuth
from app.web.templating import render

router = APIRouter()


@router.get("/")
def home(auth: UserAuth):
    """The library is the home page."""
    return RedirectResponse("/library", status_code=303)


@router.get("/nav/icons")
def nav_icons(request: Request, auth: AdminAuth):
    """The Jobs, Status and Settings icons alone; the top bar polls it to keep the colours."""
    # HTMX tells which page is open, so the right icon keeps its "current page" highlight
    current = urlsplit(request.headers.get("hx-current-url", "")).path
    return render(request, "_nav_icons.html", {"current_path": current})
