"""The audit log page (administrators only)."""

import json
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request

from app.auth.deps import DB, require_admin
from app.services import audit
from app.web.templating import render

router = APIRouter(dependencies=[Depends(require_admin)])

# Where an entry's target can be opened.
LINKS = {"request": "/requests/{id}", "artist": "/artist/{id}", "album": "/album/{id}"}


def _day(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _details(raw: str | None) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


@router.get("/admin/audit")
def audit_page(
    request: Request,
    db: DB,
    user: str = "",
    action: str = "",
    since: str = "",
    until: str = "",
    page: int = 1,
):
    action = action if action in audit.ACTIONS else ""
    result = audit.list_entries(
        db,
        actor=user,
        action=action,
        since=_day(since),
        until=_day(until),
        page=page,
    )
    query = {
        k: v
        for k, v in (
            ("user", user.strip().lower()),
            ("action", action),
            ("since", since),
            ("until", until),
        )
        if v
    }
    rows = []
    for entry in result.entries:
        link = None
        if entry.target_type in LINKS and entry.target_id:
            link = LINKS[entry.target_type].format(id=entry.target_id)
        rows.append({"e": entry, "link": link, "details": _details(entry.details_json)})
    ctx = {
        "rows": rows,
        "total": result.total,
        "page": result.page,
        "pages": result.pages,
        "actions": audit.ACTIONS,
        "actors": audit.known_actors(db),
        "f_user": user.strip().lower(),
        "f_action": action,
        "f_since": since,
        "f_until": until,
        "prev_url": "/admin/audit?" + urlencode({**query, "page": result.page - 1}),
        "next_url": "/admin/audit?" + urlencode({**query, "page": result.page + 1}),
    }
    return render(request, "admin/audit.html", ctx)
