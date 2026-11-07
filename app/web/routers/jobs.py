import math
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, require_admin
from app.jobs import engine, registry
from app.jobs.errors import JobError
from app.models import JOB_STATUSES, Job
from app.web.templating import is_htmx, render

router = APIRouter(prefix="/admin/jobs", dependencies=[Depends(require_admin)])

PER_PAGE = 25
LOG_PAGE = 500


def notify_worker(request: Request) -> None:
    worker = getattr(request.app.state, "worker", None)
    if worker is not None:
        worker.notify()


def _list_context(db: DB, type_: str, status: str, q: str, routine: str, page: int) -> dict:
    type_ = type_ if type_ in registry.job_types() else ""
    status = status if status in JOB_STATUSES else ""
    q = " ".join(q.split())[:100]
    show_routine = routine == "1"
    jobs, total = engine.list_jobs(
        db,
        type=type_ or None,
        status=status or None,
        search=q,
        routine=show_routine,
        page=page,
        per_page=PER_PAGE,
    )
    pages = max(math.ceil(total / PER_PAGE), 1)
    query = {
        k: v
        for k, v in {
            "type": type_,
            "status": status,
            "q": q,
            "routine": "1" if show_routine else "",
            "page": page,
        }.items()
        if v
    }
    return {
        "jobs": jobs,
        "total": total,
        "page": page,
        "pages": pages,
        "type": type_,
        "status": status,
        "q": q,
        "q_valid": len(q) >= engine.SEARCH_MIN,
        "show_routine": show_routine,
        "snippets": engine.matching_lines(db, [j.id for j in jobs], q),
        "job_types": registry.job_types(),
        "job_statuses": JOB_STATUSES,
        "qs": urlencode(query),
        "qs_without_page": urlencode({k: v for k, v in query.items() if k != "page"}),
    }


@router.get("")
def jobs_page(
    request: Request,
    db: DB,
    type: str = "",
    status: str = "",
    q: str = "",
    routine: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
):
    return render(request, "admin/jobs.html", _list_context(db, type, status, q, routine, page))


@router.get("/rows")
def jobs_rows(
    request: Request,
    db: DB,
    type: str = "",
    status: str = "",
    q: str = "",
    routine: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
):
    return render(
        request, "admin/_jobs_rows.html", _list_context(db, type, status, q, routine, page)
    )


def _get_job(db: DB, job_id: int) -> Job:
    job = engine.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="not_found")
    return job


@router.get("/{job_id}")
def job_detail(request: Request, db: DB, job_id: int):
    job = _get_job(db, job_id)
    lines = engine.logs_after(db, job_id, 0, LOG_PAGE)
    ctx = {"job": job, "lines": lines, "last_id": lines[-1].id if lines else 0}
    return render(request, "admin/job_detail.html", ctx)


@router.get("/{job_id}/summary")
def job_summary(request: Request, db: DB, job_id: int):
    return render(request, "admin/_job_summary.html", {"job": _get_job(db, job_id)})


@router.get("/{job_id}/logs")
def job_logs(request: Request, db: DB, job_id: int, after: Annotated[int, Query(ge=0)] = 0):
    job = _get_job(db, job_id)
    lines = engine.logs_after(db, job_id, after, LOG_PAGE)
    last_id = lines[-1].id if lines else after
    keep_polling = job.is_active or len(lines) >= LOG_PAGE
    ctx = {"job": job, "lines": lines, "last_id": last_id, "keep_polling": keep_polling}
    return render(request, "admin/_job_logs.html", ctx)


def _after_action(request: Request, db: DB, job_id: int):
    if is_htmx(request):
        return render(request, "admin/_job_row.html", {"job": _get_job(db, job_id)})
    return RedirectResponse(f"/admin/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/cancel")
def job_cancel(request: Request, db: DB, job_id: int):
    _get_job(db, job_id)
    if not engine.request_cancel(db, job_id):
        raise HTTPException(status_code=409, detail="job_not_cancellable")
    return _after_action(request, db, job_id)


@router.post("/{job_id}/retry")
def job_retry(request: Request, db: DB, job_id: int):
    _get_job(db, job_id)
    try:
        engine.retry(db, job_id)
    except JobError as exc:
        detail = "job_not_retryable"
        if str(exc) == "duplicate":
            detail = "job_duplicate"
        raise HTTPException(status_code=409, detail=detail) from exc
    notify_worker(request)
    return _after_action(request, db, job_id)
