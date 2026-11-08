"""Download requests: the list, one request in detail, the manual choice, cancel and retry."""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.auth.deps import DB, UserAuth
from app.jobs import engine
from app.models import (
    MODES,
    REQ_ACTIVE,
    REQ_AWAITING_CHOICE,
    Artist,
    DownloadFile,
    DownloadRequest,
    Job,
    ReleaseGroup,
    User,
)
from app.services import audit
from app.services import downloads as svc
from app.web.routers.auth import safe_next
from app.web.routers.jobs import notify_worker
from app.web.templating import is_htmx, render

router = APIRouter()
PAGE_SIZE = 50
RUNNING = ("queued", "searching", "candidate_found", "downloading")
# The filters of the list: name -> the statuses it shows (None: all of them).
FILTERS = {
    "all": None,
    "active": REQ_ACTIVE,
    "finished": ("completed",),
    "failed": ("failed", "no_results", "cancelled"),
}


def _get(db: DB, auth: UserAuth, request_id: int) -> DownloadRequest:
    """A request the user may see: their own (administrators see everyone's)."""
    request = db.get(DownloadRequest, request_id)
    if request is None or not (auth.user.is_admin or request.user_id == auth.user.id):
        raise HTTPException(status_code=404, detail="not_found")
    return request


def _row(db: DB, request: DownloadRequest) -> dict:
    group = db.get(ReleaseGroup, request.release_group_id)
    artist = db.get(Artist, group.artist_id)
    owner = db.get(User, request.user_id)
    return {
        "r": request,
        "group": group,
        "artist": artist,
        "owner": owner.username if owner else "",
        "active": request.status in REQ_ACTIVE,
    }


def _label(db: DB, request: DownloadRequest) -> str:
    group = db.get(ReleaseGroup, request.release_group_id)
    artist = db.get(Artist, group.artist_id) if group else None
    return f"{artist.name} – {group.title}" if artist and group else f"#{request.id}"


@router.get("/requests")
def requests_page(
    request: Request,
    db: DB,
    auth: UserAuth,
    status: str = "all",
    user: str = "",
    page: int = 1,
):
    """The list: an ordinary user sees their own requests, an administrator everybody's (and can
    pick one user). Filtered by state, 50 to a page."""
    status = status if status in FILTERS else "all"
    stmt = select(DownloadRequest)
    if not auth.user.is_admin:
        stmt = stmt.where(DownloadRequest.user_id == auth.user.id)
        user = ""
    elif user.strip():
        owner = db.scalar(select(User).where(User.username == user.strip().lower()))
        stmt = stmt.where(DownloadRequest.user_id == (owner.id if owner else -1))
    if FILTERS[status] is not None:
        stmt = stmt.where(DownloadRequest.status.in_(FILTERS[status]))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 1), pages)
    found = db.scalars(
        stmt.order_by(DownloadRequest.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    )
    rows = [_row(db, r) for r in found]
    query = {k: v for k, v in (("status", status), ("user", user.strip())) if v and v != "all"}
    return render(
        request,
        "requests.html",
        {
            "rows": rows,
            "all_users": auth.user.is_admin,
            "polling": any(r["r"].status in RUNNING for r in rows),
            "filters": list(FILTERS),
            "status": status,
            "user_filter": user.strip(),
            "users": (
                [u.username for u in db.scalars(select(User).order_by(User.username))]
                if auth.user.is_admin
                else []
            ),
            "filter_urls": {
                f: "/requests?"
                + urlencode({"status": f, **({"user": user.strip()} if user.strip() else {})})
                for f in FILTERS
            },
            "prev_url": "/requests?" + urlencode({**query, "page": page - 1}),
            "next_url": "/requests?" + urlencode({**query, "page": page + 1}),
            "page": page,
            "pages": pages,
            "total": total,
            "query": query,
            "here": "/requests"
            + (f"?{urlencode({**query, 'page': page})}" if query or page > 1 else ""),
            "can_retry": {r["r"].id: svc.can_retry(r["r"]) for r in rows},
            "can_delete": {r["r"].id: svc.can_delete(r["r"]) for r in rows},
        },
    )


def _detail_context(db: DB, auth: UserAuth, request: DownloadRequest) -> dict:
    row = _row(db, request)
    files = list(
        db.scalars(
            select(DownloadFile)
            .where(DownloadFile.request_id == request.id)
            .order_by(DownloadFile.id)
        )
    )
    job = db.scalar(
        select(Job)
        .where(
            Job.dedupe_key.in_(
                [svc.search_dedupe_key(request.id), svc.monitor_dedupe_key(request.id)]
            )
        )
        .order_by(Job.id.desc())
        .limit(1)
    )
    return {
        **row,
        "candidates": svc.candidates_of(request),
        "files": files,
        "can_delete": svc.can_delete(request),
        "job": job if auth.user.is_admin else None,
        "polling": request.status in RUNNING,
        "can_retry": svc.can_retry(request),
        "modes": MODES,
        "choosing": request.status == REQ_AWAITING_CHOICE,
    }


@router.get("/requests/{request_id}")
def request_detail(request: Request, db: DB, auth: UserAuth, request_id: int):
    return render(
        request, "request_detail.html", _detail_context(db, auth, _get(db, auth, request_id))
    )


@router.get("/requests/{request_id}/body")
def request_body(request: Request, db: DB, auth: UserAuth, request_id: int):
    """The detail without the page around it; polled while the request runs."""
    return render(
        request, "_request_body.html", _detail_context(db, auth, _get(db, auth, request_id))
    )


@router.get("/requests/{request_id}/control")
def request_control(request: Request, db: DB, auth: UserAuth, request_id: int):
    """The button/badge of the artist page, refreshed while the request runs."""
    req = _get(db, auth, request_id)
    group = db.get(ReleaseGroup, req.release_group_id)
    info = svc.request_infos(db, [group.id]).get(group.id)
    ctx = {
        "artist": db.get(Artist, group.artist_id),
        "g": group,
        "status": info.status if info else None,
        "info": info,
        "unavailable": False,
    }
    return render(request, "_request_control.html", ctx)


def _return_to(next_: str, request_id: int) -> str:
    """Where to go after an action: back to the list it was taken from, else the request."""
    target = safe_next(next_, "")
    return (
        target
        if target.startswith("/requests?") or target == "/requests"
        else f"/requests/{request_id}"
    )


def _back(request: Request, request_id: int):
    if is_htmx(request):
        return None
    return RedirectResponse(f"/requests/{request_id}", status_code=303)


@router.post("/requests/{request_id}/choose")
def request_choose(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    index: Annotated[int, Form()] = -1,
):
    """Manual mode: download the candidate the user picked."""
    req = _get(db, auth, request_id)
    if req.status != REQ_AWAITING_CHOICE:
        return RedirectResponse(
            f"/requests/{request_id}?error=request_not_choosing", status_code=303
        )
    try:
        svc.choose_candidate(db, req, index)
    except svc.RequestError as exc:
        return RedirectResponse(f"/requests/{request_id}?error={exc.code}", status_code=303)
    engine.enqueue(
        db,
        "download_monitor",
        {"request_id": req.id},
        dedupe_key=svc.monitor_dedupe_key(req.id),
        created_by=auth.user.id,
    )
    audit.record(
        db,
        auth.user,
        "request.chosen",
        target_type="request",
        target_id=req.id,
        label=_label(db, req),
        details={"candidate": index, "owner": _owner_name(db, req)},
    )
    db.commit()
    notify_worker(request)
    return RedirectResponse(f"/requests/{request_id}", status_code=303)


def _owner_name(db: DB, request: DownloadRequest) -> str:
    owner = db.get(User, request.user_id)
    return owner.username if owner else ""


@router.post("/requests/{request_id}/cancel")
def request_cancel(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    next: Annotated[str, Form()] = "",
):
    req = _get(db, auth, request_id)
    try:
        job_ids = svc.cancel_request(db, req)
    except svc.RequestError as exc:
        return RedirectResponse(f"/requests/{request_id}?error={exc.code}", status_code=303)
    for job_id in job_ids:  # stops a running job at its next check; a queued one is dropped
        engine.request_cancel(db, job_id)
    audit.record(
        db,
        auth.user,
        "request.cancelled",
        target_type="request",
        target_id=req.id,
        label=_label(db, req),
        details={"owner": _owner_name(db, req)},
    )
    db.commit()
    notify_worker(request)
    return RedirectResponse(_return_to(next, request_id), status_code=303)


@router.post("/requests/{request_id}/delete")
def request_delete(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    next: Annotated[str, Form()] = "",
):
    """Remove a finished request from the list (its owner, or an administrator). The files of
    the download, the quarantine and the library are not touched."""
    req = _get(db, auth, request_id)
    if not svc.can_delete(req):
        return RedirectResponse(
            f"/requests/{request_id}?error=request_not_deletable", status_code=303
        )
    audit.record(
        db,
        auth.user,
        "request.deleted",
        target_type="request",
        label=_label(db, req),
        details={"status": req.status, "owner": _owner_name(db, req)},
    )
    db.delete(req)
    db.commit()
    target = _return_to(next, request_id)
    if target.startswith("/requests/"):  # that page is gone: back to the list
        target = "/requests"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}flash=request_deleted", status_code=303)


@router.post("/requests/{request_id}/retry")
def request_retry(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    query: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    """Search again after a failure (optionally with another text, or choosing by hand). The new
    request belongs to the owner of the old one, also when an administrator retries it."""
    old = _get(db, auth, request_id)
    if not svc.can_retry(old):
        return RedirectResponse(
            f"/requests/{request_id}?error=request_not_retryable", status_code=303
        )
    settings = request.app.state.settings
    if not svc.slskd_configured(settings):
        return RedirectResponse(f"/requests/{request_id}?error=request_no_slskd", status_code=303)
    new, created = svc.create_request(
        db,
        old.user_id,
        old.release_group_id,
        default_mode=settings.slskd_mode,
        mode=mode or old.mode,
        quality_key=old.quality_key,
        query=query,
    )
    if created:
        engine.enqueue(
            db,
            "slskd_search",
            {"request_id": new.id},
            dedupe_key=svc.search_dedupe_key(new.id),
            created_by=auth.user.id,
        )
        audit.record(
            db,
            auth.user,
            "request.retried",
            target_type="request",
            target_id=new.id,
            label=_label(db, new),
            details={"retry_of": old.id, "owner": _owner_name(db, new)},
        )
    db.commit()
    notify_worker(request)
    return RedirectResponse(
        _return_to(next, new.id) if next else f"/requests/{new.id}", status_code=303
    )
