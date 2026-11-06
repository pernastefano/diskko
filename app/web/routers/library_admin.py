from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.auth.deps import DB, AdminAuth, require_admin
from app.jobs import engine
from app.models import ACTIVE_STATUSES, Artist, Job, LibraryAlbum, LibraryTrack
from app.services.library.quality import QUALITY_TIERS
from app.services.library.scan import last_scan
from app.services.musicbrainz.categories import CATEGORIES
from app.services.musicbrainz.missing import get_missing_types
from app.web.routers.jobs import notify_worker
from app.web.templating import render

router = APIRouter(prefix="/admin/library", dependencies=[Depends(require_admin)])

SCAN_DEDUPE_KEY = "scan_library"


@router.get("")
def library_page(request: Request, db: DB):
    settings = request.app.state.settings
    tiers = dict(
        db.execute(
            select(LibraryAlbum.quality_tier, func.count(LibraryAlbum.id)).group_by(
                LibraryAlbum.quality_tier
            )
        ).all()
    )
    active_job = db.scalar(
        select(Job.id).where(Job.dedupe_key == SCAN_DEDUPE_KEY, Job.status.in_(ACTIVE_STATUSES))
    )
    ctx = {
        "source": settings.library_source.strip().lower(),
        "music_dir": str(settings.music_dir),
        "plex_section": settings.plex_music_section,
        "cron": settings.library_scan_cron.strip(),
        "artists": db.scalar(select(func.count(Artist.id)).where(Artist.in_library.is_(True))),
        "albums": db.scalar(select(func.count(LibraryAlbum.id))),
        "tracks": db.scalar(select(func.count(LibraryTrack.id))),
        "tiers": [(t, tiers.get(t, 0)) for t in reversed(QUALITY_TIERS)],
        "last": last_scan(db),
        "active_job": active_job,
        "categories": CATEGORIES,
        "shared_types": get_missing_types(db),
    }
    return render(request, "admin/library.html", ctx)


@router.post("/scan")
def library_scan(request: Request, db: DB, auth: AdminAuth, force: Annotated[str, Form()] = ""):
    job = engine.enqueue(
        db,
        "scan_library",
        {"force": force == "1"},
        dedupe_key=SCAN_DEDUPE_KEY,
        created_by=auth.user.id,
    )
    notify_worker(request)
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=303)
