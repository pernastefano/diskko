"""Database backups (administrators only): list, back up now, download."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.auth.deps import DB, AdminAuth, require_admin
from app.jobs import engine
from app.services.backup import backup_path, list_backups
from app.web.routers.jobs import notify_worker
from app.web.templating import render

router = APIRouter(dependencies=[Depends(require_admin)])
DEDUPE_KEY = "backup_db"


@router.get("/admin/backups")
def backups_page(request: Request):
    settings = request.app.state.settings
    return render(
        request,
        "admin/backups.html",
        {
            "backups": list_backups(settings),
            "keep": max(1, settings.backup_keep),
            "cron": settings.backup_cron.strip(),
        },
    )


@router.post("/admin/backups")
def backup_now(request: Request, db: DB, auth: AdminAuth):
    job = engine.enqueue(db, "backup_db", dedupe_key=DEDUPE_KEY, created_by=auth.user.id)
    db.commit()
    notify_worker(request)
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=303)


@router.get("/admin/backups/{name}")
def download_backup(request: Request, name: str):
    path = backup_path(request.app.state.settings, name)
    if path is None:
        raise HTTPException(status_code=404, detail="not_found")
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )
