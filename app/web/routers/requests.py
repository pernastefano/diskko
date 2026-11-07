"""Download requests: the list, one request in detail, the manual choice, cancel and retry."""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

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
from app.services import downloads as svc
from app.web.routers.jobs import notify_worker
from app.web.templating import is_htmx, render

router = APIRouter()
LIST_LIMIT = 200
RUNNING = ("queued", "searching", "candidate_found", "downloading")


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


@router.get("/requests")
def requests_page(request: Request, db: DB, auth: UserAuth):
    stmt = select(DownloadRequest).order_by(DownloadRequest.id.desc()).limit(LIST_LIMIT)
    if not auth.user.is_admin:
        stmt = stmt.where(DownloadRequest.user_id == auth.user.id)
    rows = [_row(db, r) for r in db.scalars(stmt)]
    return render(
        request,
        "requests.html",
        {
            "rows": rows,
            "all_users": auth.user.is_admin,
            "polling": any(r["r"].status in RUNNING for r in rows),
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
    db.commit()
    notify_worker(request)
    return RedirectResponse(f"/requests/{request_id}", status_code=303)


@router.post("/requests/{request_id}/cancel")
def request_cancel(request: Request, db: DB, auth: UserAuth, request_id: int):
    req = _get(db, auth, request_id)
    try:
        job_ids = svc.cancel_request(db, req)
    except svc.RequestError as exc:
        return RedirectResponse(f"/requests/{request_id}?error={exc.code}", status_code=303)
    for job_id in job_ids:  # stops a running job at its next check; a queued one is dropped
        engine.request_cancel(db, job_id)
    db.commit()
    notify_worker(request)
    return RedirectResponse(f"/requests/{request_id}", status_code=303)


@router.post("/requests/{request_id}/retry")
def request_retry(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    query: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "",
):
    """Search again after a failure (optionally with another text, or choosing by hand)."""
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
        auth.user.id,
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
        db.commit()
        notify_worker(request)
    return RedirectResponse(f"/requests/{new.id}", status_code=303)
