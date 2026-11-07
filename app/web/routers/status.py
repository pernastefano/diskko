from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth.deps import DB, AdminAuth, require_admin
from app.integrations.status import (
    INTEGRATION_NAMES,
    build_integration,
    check_dedupe_key,
    check_in_progress,
    integration_health,
    last_result,
)
from app.jobs import engine, registry
from app.models import ACTIVE_STATUSES, Job
from app.web.routers.jobs import notify_worker
from app.web.templating import is_htmx, render

router = APIRouter(prefix="/admin/status", dependencies=[Depends(require_admin)])


def integration_card(request: Request, db: DB, name: str) -> dict:
    integration = build_integration(name, request.app.state.settings)
    if integration is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {
        "name": name,
        "configured": integration.is_configured(),
        "result": last_result(db, name),
        "checking": check_in_progress(db, name),
    }


CHECK_ALL_KEY = "scheduled:check_integrations"  # the scheduler's own key: never two at once


def _grid_context(request: Request, db: DB) -> dict:
    settings = request.app.state.settings
    checking_all = (
        db.scalar(
            select(Job.id).where(Job.dedupe_key == CHECK_ALL_KEY, Job.status.in_(ACTIVE_STATUSES))
        )
        is not None
    )
    return {
        "cards": [integration_card(request, db, n) for n in INTEGRATION_NAMES],
        "health": integration_health(db, settings),
        "check_minutes": settings.status_check_minutes,
        "checking_all": checking_all,
    }


@router.get("/grid")
def status_grid(request: Request, db: DB):
    """The cards and the summary alone; the page refreshes them by itself."""
    return render(request, "admin/_status_grid.html", _grid_context(request, db))


@router.post("/test-all")
def status_test_all(request: Request, db: DB, auth: AdminAuth):
    engine.enqueue(db, "check_integrations", {}, dedupe_key=CHECK_ALL_KEY, created_by=auth.user.id)
    notify_worker(request)
    if is_htmx(request):
        return render(request, "admin/_status_grid.html", _grid_context(request, db))
    return RedirectResponse("/admin/status", status_code=303)


@router.get("")
def status_page(request: Request, db: DB):
    settings = request.app.state.settings
    stats = engine.queue_stats(db)
    concurrency = settings.queue_concurrency
    queues = [
        {
            "name": q,
            "concurrency": concurrency.get(q, 1),
            "queued": stats.get(q, {}).get("queued", 0),
            "running": stats.get(q, {}).get("running", 0),
        }
        for q in registry.queue_names()
    ]
    ctx = {
        **_grid_context(request, db),
        "queues": queues,
        "workers_enabled": request.app.state.worker is not None,
    }
    return render(request, "admin/status.html", ctx)


@router.get("/{name}/card")
def status_card(request: Request, db: DB, name: str):
    return render(request, "admin/_status_card.html", {"card": integration_card(request, db, name)})


@router.post("/{name}/test")
def status_test(request: Request, db: DB, auth: AdminAuth, name: str):
    integration_card(request, db, name)  # 404 for unknown integrations
    engine.enqueue(
        db,
        "integration_check",
        {"name": name},
        dedupe_key=check_dedupe_key(name),
        created_by=auth.user.id,
    )
    notify_worker(request)
    if is_htmx(request):
        return render(
            request, "admin/_status_card.html", {"card": integration_card(request, db, name)}
        )
    return RedirectResponse("/admin/status", status_code=303)
