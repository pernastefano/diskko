"""Admin pages for MusicBrainz identities, album matching and missing albums."""

import math
import re
from collections import defaultdict
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, func, select

from app.auth.deps import DB, AdminAuth, require_admin
from app.i18n import has_key
from app.jobs import engine
from app.jobs.handlers.sync_artist import sync_dedupe_key
from app.models import (
    MATCH_MANUAL,
    MATCH_NONE,
    MB_STATUSES,
    AlbumMatchOverride,
    Artist,
    ArtistReview,
    LibraryAlbum,
    ReleaseGroup,
)
from app.services.library.tags import clean_mbid
from app.services.musicbrainz import sync
from app.services.musicbrainz.matching import candidates_for_review, match_artist_albums
from app.services.musicbrainz.missing import (
    get_missing_types,
    missing_counts,
    missing_release_groups,
)
from app.services.musicbrainz.titles import album_key
from app.web.routers.jobs import notify_worker
from app.web.templating import render

router = APIRouter(prefix="/admin/artists", dependencies=[Depends(require_admin)])

PER_PAGE = 50
_UUID_IN_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def parse_mbid_input(raw: str) -> str | None:
    """Accept an MBID or a full musicbrainz.org URL containing one."""
    found = _UUID_IN_TEXT.search(raw or "")
    return clean_mbid(found.group(0)) if found else None


def _error(request: Request) -> str | None:
    key = f"error.{request.query_params.get('error', '')}"
    return key if has_key(key) else None


def _artist_or_404(db: DB, artist_id: int) -> Artist:
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(status_code=404, detail="not_found")
    return artist


def queue_sync(request: Request, db: DB, artist_id: int, user_id: int, force: bool = False):
    if not request.app.state.settings.mb_user_agent.strip():
        raise HTTPException(status_code=409, detail="mb_not_configured")
    job = engine.enqueue(
        db,
        "sync_artist_metadata",
        {"artist_id": artist_id, "force": force},
        dedupe_key=sync_dedupe_key(artist_id),
        created_by=user_id,
    )
    notify_worker(request)
    return job


# ------------------------------------------------------------------------ list


@router.get("")
def artists_page(
    request: Request,
    db: DB,
    auth: AdminAuth,
    q: str = "",
    status: str = "",
    attention: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
):
    status = status if status in MB_STATUSES else ""
    stmt = select(Artist).where(Artist.in_library.is_(True))
    if status:
        stmt = stmt.where(Artist.mb_status == status)
    if q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(Artist.name.ilike(like, escape="\\"))
    if attention == "1":  # artists with albums that need a look
        stmt = stmt.where(
            select(LibraryAlbum.id)
            .where(
                LibraryAlbum.artist_id == Artist.id,
                LibraryAlbum.match_status.in_(("tentative", "unmatched")),
            )
            .exists()
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    artists = list(
        db.scalars(
            stmt.order_by(Artist.sort_name, Artist.id).limit(PER_PAGE).offset((page - 1) * PER_PAGE)
        )
    )
    ids = [a.id for a in artists]
    album_counts: dict[int, dict[str, int]] = defaultdict(dict)
    if ids:
        for artist_id, match_status, n in db.execute(
            select(LibraryAlbum.artist_id, LibraryAlbum.match_status, func.count(LibraryAlbum.id))
            .where(LibraryAlbum.artist_id.in_(ids))
            .group_by(LibraryAlbum.artist_id, LibraryAlbum.match_status)
        ):
            album_counts[artist_id][match_status] = n
    types = get_missing_types(db)
    query = {
        k: v for k, v in {"q": q.strip(), "status": status, "attention": attention}.items() if v
    }
    ctx = {
        "artists": artists,
        "album_counts": album_counts,
        "missing": missing_counts(db, ids, types),
        "total": total,
        "page": page,
        "pages": max(math.ceil(total / PER_PAGE), 1),
        "q": q,
        "status": status,
        "attention": attention,
        "statuses": MB_STATUSES,
        "qs": urlencode(query),
        "pending_reviews": db.scalar(
            select(func.count(ArtistReview.id)).where(ArtistReview.status == "pending")
        ),
    }
    return render(request, "admin/artists.html", ctx)


# ---------------------------------------------------------------------- review


@router.get("/review")
def review_page(request: Request, db: DB):
    reviews = list(
        db.execute(
            select(ArtistReview, Artist)
            .join(Artist, Artist.id == ArtistReview.artist_id)
            .where(ArtistReview.status == "pending")
            .order_by(Artist.sort_name, ArtistReview.id)
        )
    )
    items = [
        {"review": review, "artist": artist, "candidates": sync.pending_review_candidates(review)}
        for review, artist in reviews
    ]
    return render(request, "admin/artist_review.html", {"items": items, "error": _error(request)})


# ---------------------------------------------------------------------- detail


@router.get("/{artist_id}")
def artist_page(request: Request, db: DB, auth: AdminAuth, artist_id: int):
    artist = _artist_or_404(db, artist_id)
    groups = list(db.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == artist_id)))
    by_id = {g.id: g for g in groups}
    albums = list(
        db.scalars(
            select(LibraryAlbum)
            .where(LibraryAlbum.artist_id == artist_id)
            .order_by(LibraryAlbum.year.is_(None), LibraryAlbum.year, LibraryAlbum.title)
        )
    )
    rows = [
        {
            "album": album,
            "group": by_id.get(album.release_group_id),
            "choices": [(g, round(c * 100)) for g, c in candidates_for_review(db, album, limit=8)],
        }
        for album in albums
    ]
    types = get_missing_types(db)
    review = db.scalar(
        select(ArtistReview).where(
            ArtistReview.artist_id == artist_id, ArtistReview.status == "pending"
        )
    )
    ctx = {
        "artist": artist,
        "rows": rows,
        "missing": missing_release_groups(db, artist_id, types),
        "types": types,
        "group_count": len(groups),
        "review": review,
        "mb_configured": bool(request.app.state.settings.mb_user_agent.strip()),
        "error": _error(request),
    }
    return render(request, "admin/artist_detail.html", ctx)


# --------------------------------------------------------------------- actions


@router.post("/{artist_id}/sync")
def artist_sync(
    request: Request, db: DB, auth: AdminAuth, artist_id: int, force: Annotated[str, Form()] = ""
):
    _artist_or_404(db, artist_id)
    job = queue_sync(request, db, artist_id, auth.user.id, force == "1")
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=303)


@router.post("/{artist_id}/resolve")
def artist_resolve(
    request: Request,
    db: DB,
    auth: AdminAuth,
    artist_id: int,
    mbid: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    _artist_or_404(db, artist_id)
    cleaned = parse_mbid_input(mbid)
    back = "/admin/artists/review" if next == "review" else f"/admin/artists/{artist_id}"
    if cleaned is None:
        return RedirectResponse(f"{back}?error=mbid_invalid", status_code=303)
    sync.choose_candidate(db, artist_id, cleaned, auth.user.id)
    db.commit()
    artist = db.get(Artist, artist_id)
    if artist.mbid == cleaned:  # not refused as a duplicate
        queue_sync(request, db, artist_id, auth.user.id)
        return RedirectResponse(f"{back}?flash=review_resolved", status_code=303)
    return RedirectResponse(f"{back}?error=mbid_in_use", status_code=303)


@router.post("/{artist_id}/dismiss")
def artist_dismiss(db: DB, auth: AdminAuth, artist_id: int, next: Annotated[str, Form()] = ""):
    _artist_or_404(db, artist_id)
    sync.dismiss_artist(db, artist_id, auth.user.id)
    db.commit()
    back = "/admin/artists/review" if next == "review" else f"/admin/artists/{artist_id}"
    return RedirectResponse(f"{back}?flash=artist_dismissed", status_code=303)


@router.post("/{artist_id}/reset")
def artist_reset(request: Request, db: DB, auth: AdminAuth, artist_id: int):
    _artist_or_404(db, artist_id)
    sync.reset_artist(db, artist_id, auth.user.id)
    db.commit()
    queue_sync(request, db, artist_id, auth.user.id, force=True)
    return RedirectResponse(f"/admin/artists/{artist_id}?flash=artist_reset", status_code=303)


@router.post("/{artist_id}/albums/{album_id}/match")
def album_match(
    db: DB,
    auth: AdminAuth,
    artist_id: int,
    album_id: int,
    choice: Annotated[str, Form()] = "auto",
):
    """Manual override: a release group, 'none' (not on MusicBrainz) or 'auto' (undo)."""
    artist = _artist_or_404(db, artist_id)
    album = db.get(LibraryAlbum, album_id)
    if album is None or album.artist_id != artist.id:
        raise HTTPException(status_code=404, detail="not_found")
    key = album_key(album.title)
    existing = db.scalar(
        select(AlbumMatchOverride).where(
            and_(AlbumMatchOverride.artist_id == artist_id, AlbumMatchOverride.album_key == key)
        )
    )
    if choice == "auto":
        if existing is not None:
            db.delete(existing)
    else:
        group_id = None
        if choice != "none":
            group = db.get(ReleaseGroup, int(choice)) if choice.isdigit() else None
            if group is None or group.artist_id != artist_id:
                raise HTTPException(status_code=400, detail="rg_invalid")
            group_id = group.id
        if existing is None:
            existing = AlbumMatchOverride(artist_id=artist_id, album_key=key)
            db.add(existing)
        existing.release_group_id, existing.created_by = group_id, auth.user.id
    db.flush()
    match_artist_albums(db, artist_id)
    db.commit()
    return RedirectResponse(f"/admin/artists/{artist_id}?flash=match_saved", status_code=303)


__all__ = ["MATCH_MANUAL", "MATCH_NONE", "router"]
