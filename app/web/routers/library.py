"""The library as users see it: artist grid/list with search, filters and infinite scroll."""

import math
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth.deps import DB, UserAuth
from app.config import Settings
from app.jobs import engine
from app.jobs.handlers.sync_artist import sync_dedupe_key
from app.models import ACTIVE_STATUSES, Artist, Job, ReleaseGroup, utcnow
from app.services import audit
from app.services import downloads as svc
from app.services.downloads import request_infos, slskd_configured
from app.services.library_views import (
    ALBUM_SORTS,
    PAGE_SIZE,
    SORTS,
    album_facets,
    available_letters,
    build_artist_sections,
    list_albums,
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
        types=get_missing_types(db),
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


@router.get("/artists")
def artists_page(
    request: Request, db: DB, auth: UserAuth, params: Annotated[LibraryParams, Depends(_params)]
):
    ctx = _context(db, auth, params)
    ctx["letters"] = available_letters(db)
    ctx["alphabet"] = [*"ABCDEFGHIJKLMNOPQRSTUVWXYZ", "#"]
    return render(request, "artists.html", ctx)


@router.get("/artists/items")
def artist_items(
    request: Request, db: DB, auth: UserAuth, params: Annotated[LibraryParams, Depends(_params)]
):
    """Tiles of one page (HTMX): used by the search box and by the infinite scroll."""
    return render(request, "_artist_items.html", _context(db, auth, params))


# ---------------------------------------------------------------------- album list


class AlbumParams:
    """Normalized filters of the albums page; builds the query strings of its links."""

    def __init__(self, q: str, quality: str, fmt: str, category: str, sort: str, page: int) -> None:
        self.q = q.strip()[:100]
        self.quality = quality.strip()[:48]
        self.fmt = fmt.strip().lower()[:16]
        self.category = category.strip()[:24]
        self.sort = sort if sort in ALBUM_SORTS else "title"
        self.page = max(page, 1)

    @property
    def filtered(self) -> bool:
        return bool(self.q or self.quality or self.fmt or self.category)

    def query(self, **overrides: object) -> str:
        values = {
            "q": self.q,
            "quality": self.quality,
            "fmt": self.fmt,
            "category": self.category,
            "sort": self.sort if self.sort != "title" else "",
            "page": self.page if self.page > 1 else "",
        }
        values.update(overrides)
        return urlencode({k: v for k, v in values.items() if v not in ("", None)})


def _album_params(
    q: str = "",
    quality: str = "",
    fmt: str = "",
    category: str = "",
    sort: str = "title",
    page: int = 1,
) -> AlbumParams:
    return AlbumParams(q, quality, fmt, category, sort, page)


def _album_context(request: Request, db: DB, params: AlbumParams) -> dict:
    profiles = request.app.state.quality_profiles
    rows, total = list_albums(
        db,
        profiles,
        q=params.q,
        quality=params.quality,
        fmt=params.fmt,
        category=params.category,
        sort=params.sort,
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
    }


@router.get("/albums")
def albums_page(
    request: Request, db: DB, auth: UserAuth, params: Annotated[AlbumParams, Depends(_album_params)]
):
    ctx = _album_context(request, db, params)
    ctx["profiles"] = list(reversed(request.app.state.quality_profiles.profiles))
    ctx["sorts"] = ALBUM_SORTS
    ctx.update(album_facets(db))
    return render(request, "albums.html", ctx)


@router.get("/albums/items")
def album_items(
    request: Request, db: DB, auth: UserAuth, params: Annotated[AlbumParams, Depends(_album_params)]
):
    """Tiles of one page (HTMX): used by the search box and by the infinite scroll."""
    return render(request, "_album_items.html", _album_context(request, db, params))


# ------------------------------------------------------------------ artist page

COVER_RETRY_MINUTES = 10


def _artist_or_404(db: DB, artist_id: int) -> Artist:
    artist = db.get(Artist, artist_id)
    if artist is None:  # artists opened from a search (not in the library) have a page too
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
        {"artist_id": artist.id},
        dedupe_key=key,
        created_by=auth.user.id,
    )
    notify_worker(request)


def _sync_active(db: DB, artist_id: int) -> bool:
    return (
        db.scalar(
            select(Job.id).where(
                Job.dedupe_key == sync_dedupe_key(artist_id), Job.status.in_(ACTIVE_STATUSES)
            )
        )
        is not None
    )


def _artist_context(
    db: DB, auth: UserAuth, artist: Artist, show_all: bool, settings: Settings
) -> dict:
    types = get_missing_types(db)
    sections = build_artist_sections(db, artist.id, types, show_all=show_all)
    group_ids = [g.id for _, items in sections.missing for g in items]
    group_ids += [i.group.id for _, items in sections.owned for i in items if i.group]
    return {
        "artist": artist,
        "sections": sections,
        "types": types,
        "show_all": show_all,
        "statuses": request_statuses(db, auth.user.id, group_ids),
        "infos": request_infos(db, group_ids),
        "slskd_ready": slskd_configured(settings),
        "covers_active": _cover_job_active(db, artist.id),
        "syncing": _sync_active(db, artist.id),
        "unavailable": set(),
    }


@router.get("/artist/{artist_id}")
def artist_page(request: Request, db: DB, auth: UserAuth, artist_id: int, all: str = ""):
    artist = _artist_or_404(db, artist_id)
    ctx = _artist_context(db, auth, artist, all == "1", request.app.state.settings)
    _ensure_covers(request, db, auth, artist, ctx["sections"], ctx["types"])
    ctx["covers_active"] = _cover_job_active(db, artist_id)
    ctx["syncing"] = _sync_active(db, artist_id)
    ctx["mb_configured"] = bool(request.app.state.settings.mb_user_agent.strip())
    return render(request, "artist.html", ctx)


@router.get("/artist/{artist_id}/sections")
def artist_sections(request: Request, db: DB, auth: UserAuth, artist_id: int, all: str = ""):
    """The two lists alone; polled while covers are still downloading."""
    artist = _artist_or_404(db, artist_id)
    return render(
        request,
        "_artist_sections.html",
        _artist_context(db, auth, artist, all == "1", request.app.state.settings),
    )


@router.post("/artist/{artist_id}/request/{group_id}")
def request_download(request: Request, db: DB, auth: UserAuth, artist_id: int, group_id: int):
    """Ask for an album: a search job finds it on Soulseek and downloads the best candidate."""
    _artist_or_404(db, artist_id)
    group = db.get(ReleaseGroup, group_id)
    if group is None or group.artist_id != artist_id:
        raise HTTPException(status_code=404, detail="not_found")
    settings = request.app.state.settings
    if not slskd_configured(settings):
        if is_htmx(request):
            ctx = {
                "artist": db.get(Artist, artist_id),
                "g": group,
                "status": None,
                "unavailable": True,
            }
            return render(request, "_request_control.html", ctx)
        return RedirectResponse(f"/artist/{artist_id}?flash=request_unavailable", status_code=303)
    created_request, created = svc.create_request(
        db, auth.user.id, group_id, default_mode=settings.slskd_mode
    )
    if created:
        engine.enqueue(
            db,
            "slskd_search",
            {"request_id": created_request.id},
            dedupe_key=svc.search_dedupe_key(created_request.id),
            created_by=auth.user.id,
        )
        artist = db.get(Artist, artist_id)
        audit.record(
            db,
            auth.user,
            "request.created",
            target_type="request",
            target_id=created_request.id,
            label=f"{artist.name} – {group.title}",
            details={"quality": created_request.quality_key},
        )
        db.commit()
        notify_worker(request)
    if is_htmx(request):
        info = svc.request_infos(db, [group_id]).get(group_id)
        ctx = {
            "artist": db.get(Artist, artist_id),
            "g": group,
            "status": info.status if info else None,
            "info": info,
            "unavailable": False,
        }
        return render(request, "_request_control.html", ctx)
    return RedirectResponse(f"/artist/{artist_id}?flash=request_created", status_code=303)
