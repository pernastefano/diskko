"""In-app notifications: the bell in the header and the page that lists them."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, UserAuth
from app.services import notifications as svc
from app.web.templating import is_htmx, render

router = APIRouter()
MENU_SIZE = 5  # notifications in the bell's menu


@router.get("/nav/notifications")
def bell(request: Request, db: DB, auth: UserAuth):
    """The bell alone, with its menu of the latest five. The top bar asks for it when a page
    opens, every 30 seconds and after the user's actions."""
    return _bell(request, db, auth.user.id)


def _bell(request: Request, db: DB, user_id: int):
    ctx = {
        "unread": svc.unread_count(db, user_id),
        "notes": svc.list_for(db, user_id, MENU_SIZE),
    }
    return render(request, "_notif_bell.html", ctx)


@router.get("/notifications")
def notifications_page(request: Request, db: DB, auth: UserAuth):
    notes = svc.list_for(db, auth.user.id)
    return render(
        request,
        "notifications.html",
        {"notes": notes, "unread": sum(1 for n in notes if n.read_at is None)},
    )


@router.post("/notifications/read")
def notifications_read(
    request: Request, db: DB, auth: UserAuth, id: Annotated[int | None, Form()] = None
):
    """Mark one notification (or all of them) as read. Only the user's own."""
    svc.mark_read(db, auth.user.id, id)
    db.commit()
    if is_htmx(request):  # from the bell's menu: the bell comes back updated
        return _bell(request, db, auth.user.id)
    return RedirectResponse("/notifications", status_code=303)
