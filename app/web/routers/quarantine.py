"""The quarantine page (administrators): look, take a download back to match it by hand, delete."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, AdminAuth, require_admin
from app.jobs import engine
from app.models import ReleaseGroup
from app.services import audit
from app.services import downloads as svc
from app.services import quarantine as quarantine_svc
from app.services.quarantine import QuarantineError
from app.web.routers.jobs import notify_worker
from app.web.templating import render

router = APIRouter(prefix="/admin/quarantine", dependencies=[Depends(require_admin)])


def _label(db: DB, entry: quarantine_svc.Entry) -> str:
    if entry.request is None:
        return ""
    group = db.get(ReleaseGroup, entry.request.release_group_id)
    return group.title if group else ""


@router.get("")
def quarantine_page(request: Request, db: DB, auth: AdminAuth):
    entries = quarantine_svc.list_entries(request.app.state.settings, db)
    albums = {e.name: _label(db, e) for e in entries}
    return render(request, "admin/quarantine.html", {"entries": entries, "albums": albums})


def _back(flash: str = "", error: str = ""):
    query = f"?flash={flash}" if flash else f"?error={error}" if error else ""
    return RedirectResponse(f"/admin/quarantine{query}", status_code=303)


@router.post("/delete")
def quarantine_delete(
    request: Request,
    db: DB,
    auth: AdminAuth,
    names: Annotated[list[str] | None, Form()] = None,
):
    """Delete the folders chosen: gone for good (what is in them is not kept anywhere else)."""
    settings = request.app.state.settings
    removed = 0
    for name in names or []:
        try:
            quarantine_svc.delete(settings, name)
        except (QuarantineError, OSError):
            continue
        removed += 1
        audit.record(
            db,
            auth.user,
            "quarantine.deleted",
            target_type="quarantine",
            label=name,
        )
    db.commit()
    return _back(
        flash="quarantine_deleted" if removed else "", error="" if removed else "quarantine_nothing"
    )


@router.post("/{name}/reimport")
def quarantine_reimport(request: Request, db: DB, auth: AdminAuth, name: str):
    """Take the download back and match its files with MusicBrainz by hand (its request asks)."""
    settings = request.app.state.settings
    profiles = request.app.state.quality_profiles
    lowest = min(profiles.profiles, key=lambda p: p.rank).key
    try:
        req = quarantine_svc.restore(settings, db, name, lowest)
    except QuarantineError as exc:
        db.rollback()
        return _back(error=exc.code)
    engine.enqueue(
        db,
        "postprocess",
        {"request_id": req.id, "release_choice": svc.RELEASE_RECONCILE},
        dedupe_key=svc.postprocess_dedupe_key(req.id),
        created_by=auth.user.id,
    )
    audit.record(
        db, auth.user, "quarantine.reimported", target_type="request", target_id=req.id, label=name
    )
    db.commit()
    notify_worker(request)
    return RedirectResponse(f"/requests/{req.id}", status_code=303)
