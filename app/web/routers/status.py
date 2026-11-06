from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, AdminAuth, require_admin
from app.integrations.status import (
    INTEGRATION_NAMES,
    build_integration,
    check_dedupe_key,
    check_in_progress,
    last_result,
)
from app.jobs import engine, registry
from app.web.routers.jobs import notify_worker
from app.web.templating import is_htmx, render

router = APIRouter(prefix="/admin/status", dependencies=[Depends(require_admin)])


def _card(request: Request, db: DB, name: str) -> dict:
    integration = build_integration(name, request.app.state.settings)
    if integration is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {
        "name": name,
        "configured": integration.is_configured(),
        "result": last_result(db, name),
        "checking": check_in_progress(db, name),
    }


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
        "cards": [_card(request, db, n) for n in INTEGRATION_NAMES],
        "queues": queues,
        "workers_enabled": request.app.state.worker is not None,
    }
    return render(request, "admin/status.html", ctx)


@router.get("/{name}/card")
def status_card(request: Request, db: DB, name: str):
    return render(request, "admin/_status_card.html", {"card": _card(request, db, name)})


@router.post("/{name}/test")
def status_test(request: Request, db: DB, auth: AdminAuth, name: str):
    _card(request, db, name)  # 404 for unknown integrations
    engine.enqueue(
        db,
        "integration_check",
        {"name": name},
        dedupe_key=check_dedupe_key(name),
        created_by=auth.user.id,
    )
    notify_worker(request)
    if is_htmx(request):
        return render(request, "admin/_status_card.html", {"card": _card(request, db, name)})
    return RedirectResponse("/admin/status", status_code=303)
