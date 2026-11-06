"""The library as users see it: artist grid/list with search, filters and infinite scroll."""

import math
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth.deps import DB, UserAuth
from app.jobs import engine
from app.models import ACTIVE_STATUSES, Artist, Job, ReleaseGroup, utcnow
from app.services.library_views import (
    PAGE_SIZE,
    SORTS,
    available_letters,
    build_artist_sections,
    list_artists,
    request_statuses,
)
from app.services.musicbrainz.missing import get_missing_types
from app.web.routers.jobs import notify_worker
from app.web.templating import is_htmx, render

router = APIRouter()

VIEWS = ("grid", "list")


class LibraryParams:
    """Normalized filters of the library page; builds the query strings of its links."""

    def __init__(self, q: str, letter: str, sort: str, missing: str, view: str, page: int) -> None:
        self.q = q.strip()[:100]
        initial = letter.strip()[:1]
        self.letter = (
            initial.upper() if initial == "#" or (initial.isascii() and initial.isalpha()) else ""
        )
        self.sort = sort if sort in SORTS else "name"
        self.only_missing = missing == "1"
        self.view = view if view in VIEWS else "grid"
        self.page = max(page, 1)

    def query(self, **overrides: object) -> str:
        values = {
            "q": self.q,
            "letter": self.letter,
            "sort": self.sort if self.sort != "name" else "",
            "missing": "1" if self.only_missing else "",
            "view": self.view if self.view != "grid" else "",
            "page": self.page if self.page > 1 else "",
        }
        values.update(overrides)
        return urlencode({k: v for k, v in values.items() if v not in ("", None)})


def _params(
    q: str = "",
    letter: str = "",
    sort: str = "name",
    missing: str = "",
    view: str = "grid",
    page: int = 1,
) -> LibraryParams:
    return LibraryParams(q, letter, sort, missing, view, page)


def _context(db: DB, auth: UserAuth, params: LibraryParams) -> dict:
    rows, total = list_artists(
        db,
        types=get_missing_types(db, auth.user.id),
        q=params.q,
        letter=params.letter,
        sort=params.sort,
        only_missing=params.only_missing,
        page=params.page,
    )
    pages = max(math.ceil(total / PAGE_SIZE), 1)
    return {
        "rows": rows,
        "total": total,
        "page": params.page,
        "pages": pages,
        "p": params,
        "next_query": params.query(page=params.page + 1) if params.page < pages else None,
        "prev_query": params.query(page=params.page - 1 if params.page > 2 else "")
        if params.page > 1
        else None,
    }


@router.get("/library")
def library_page(
    request: Request, db: DB, auth: UserAuth, params: Annotated[LibraryParams, Depends(_params)]
):
    ctx = _context(db, auth, params)
    ctx["letters"] = available_letters(db)
    ctx["alphabet"] = [*"ABCDEFGHIJKLMNOPQRSTUVWXYZ", "#"]
    return render(request, "library.html", ctx)


@router.get("/library/items")
def library_items(
    request: Request, db: DB, auth: UserAuth, params: Annotated[LibraryParams, Depends(_params)]
):
    """Tiles of one page (HTMX): used by the search box and by the infinite scroll."""
    return render(request, "_library_items.html", _context(db, auth, params))


# ------------------------------------------------------------------ artist page

COVER_RETRY_MINUTES = 10


def _artist_or_404(db: DB, artist_id: int) -> Artist:
    artist = db.get(Artist, artist_id)
    if artist is None or not artist.in_library:
        raise HTTPException(status_code=404, detail="not_found")
    return artist


def _cover_job_active(db: DB, artist_id: int) -> bool:
    return (
        db.scalar(
            select(Job.id).where(
                Job.dedupe_key == f"fetch_covers:{artist_id}", Job.status.in_(ACTIVE_STATUSES)
            )
        )
        is not None
    )


def _ensure_covers(
    request: Request, db: DB, auth: UserAuth, artist: Artist, sections, types
) -> None:
    """Queue the download of the covers this page is missing (once, not in a tight loop)."""
    settings = request.app.state.settings
    if not (settings.coverart_enabled and sections.covers_pending and artist.metadata_synced_at):
        return
    key = f"fetch_covers:{artist.id}"
    if _cover_job_active(db, artist.id):
        return
    recent = db.scalar(
        select(Job.id).where(
            Job.dedupe_key == key,
            Job.finished_at > utcnow() - timedelta(minutes=COVER_RETRY_MINUTES),
        )
    )
    if recent is not None:  # it just ran: whatever is still missing failed or has no cover
        return
    engine.enqueue(
        db,
        "fetch_covers",
        {"artist_id": artist.id, "types": list(types)},
        dedupe_key=key,
        created_by=auth.user.id,
    )
    notify_worker(request)


def _artist_context(db: DB, auth: UserAuth, artist: Artist, show_all: bool) -> dict:
    types = get_missing_types(db, auth.user.id)
    sections = build_artist_sections(db, artist.id, types, show_all=show_all)
    group_ids = [g.id for _, items in sections.missing for g in items]
    group_ids += [i.group.id for _, items in sections.owned for i in items if i.group]
    return {
        "artist": artist,
        "sections": sections,
        "types": types,
        "show_all": show_all,
        "statuses": request_statuses(db, auth.user.id, group_ids),
        "covers_active": _cover_job_active(db, artist.id),
        "unavailable": set(),
    }


@router.get("/artist/{artist_id}")
def artist_page(request: Request, db: DB, auth: UserAuth, artist_id: int, all: str = ""):
    artist = _artist_or_404(db, artist_id)
    ctx = _artist_context(db, auth, artist, show_all=all == "1")
    _ensure_covers(request, db, auth, artist, ctx["sections"], ctx["types"])
    ctx["covers_active"] = _cover_job_active(db, artist_id)
    ctx["mb_configured"] = bool(request.app.state.settings.mb_user_agent.strip())
    return render(request, "artist.html", ctx)


@router.get("/artist/{artist_id}/sections")
def artist_sections(request: Request, db: DB, auth: UserAuth, artist_id: int, all: str = ""):
    """The two lists alone; polled while covers are still downloading."""
    artist = _artist_or_404(db, artist_id)
    return render(request, "_artist_sections.html", _artist_context(db, auth, artist, all == "1"))


@router.post("/artist/{artist_id}/request/{group_id}")
def request_download(request: Request, db: DB, auth: UserAuth, artist_id: int, group_id: int):
    """Placeholder: the real request flow arrives with the download phases."""
    _artist_or_404(db, artist_id)
    group = db.get(ReleaseGroup, group_id)
    if group is None or group.artist_id != artist_id:
        raise HTTPException(status_code=404, detail="not_found")
    if is_htmx(request):
        ctx = {"artist": db.get(Artist, artist_id), "g": group, "status": None, "unavailable": True}
        return render(request, "_request_control.html", ctx)
    return RedirectResponse(f"/artist/{artist_id}?flash=request_unavailable", status_code=303)
