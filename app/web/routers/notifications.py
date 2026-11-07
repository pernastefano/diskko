"""In-app notifications: the bell in the header and the page that lists them."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, UserAuth
from app.services import notifications as svc
from app.web.templating import render

router = APIRouter()


@router.get("/nav/notifications")
def bell(request: Request, db: DB, auth: UserAuth):
    """The bell alone; the top bar asks for it when a page opens and then every 30 seconds."""
    return render(request, "_notif_bell.html", {"unread": svc.unread_count(db, auth.user.id)})


@router.get("/notifications")
def notifications_page(request: Request, db: DB, auth: UserAuth):
    notes = svc.list_for(db, auth.user.id)
    return render(
        request,
        "notifications.html",
        {"notes": notes, "unread": sum(1 for n in notes if n.read_at is None)},
    )


@router.post("/notifications/read")
def notifications_read(db: DB, auth: UserAuth, id: Annotated[int | None, Form()] = None):
    """Mark one notification (or all of them) as read. Only the user's own."""
    svc.mark_read(db, auth.user.id, id)
    db.commit()
    return RedirectResponse("/notifications", status_code=303)
