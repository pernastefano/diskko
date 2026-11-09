"""Matching one library album with MusicBrainz by hand (administrators only).

A page of its own for the album: the artist's release groups ranked for it, a search over all of
MusicBrainz (the album's title, a free text or a Lucene query, in the artist's discography or
anywhere), and the decisions "not on MusicBrainz" and "back to automatic". The search itself is a
job; the page only reads its cached answer and polls while it runs.
"""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth.deps import DB, AdminAuth, require_admin
from app.integrations.musicbrainz_client import (
    ReleaseGroupHit,
    peek_release_group_search,
    release_group_query,
)
from app.jobs import engine
from app.jobs.handlers.fetch_tracklist import tracklist_dedupe_key
from app.jobs.handlers.search_release_groups import search_dedupe_key
from app.models import Artist, LibraryAlbum, LibraryTrack, ReleaseGroup, TracklistFetch
from app.services import album_artist
from app.services import album_compare as compare_svc
from app.services import album_match as svc
from app.services.tracklists import load_tracks
from app.web.routers.discovery import job_state
from app.web.routers.jobs import notify_worker
from app.web.templating import render

router = APIRouter(prefix="/admin/albums", dependencies=[Depends(require_admin)])

MODES = ("title", "anything")
SCOPES = ("artist", "all")
LOCAL_SHOWN = 6  # artist release groups shown before "show all"


def _album_or_404(db: DB, album_id: int) -> LibraryAlbum:
    album = db.get(LibraryAlbum, album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="not_found")
    return album


class SearchParams:
    """What was asked for: the text, how to read it and where to look."""

    def __init__(self, artist: Artist, q: str = "", mode: str = "title", scope: str = "artist"):
        self.q = q.strip()[:200]
        self.mode = mode if mode in MODES else "title"
        can_limit = bool(artist.mbid)
        self.scope = scope if scope in SCOPES and (scope == "all" or can_limit) else "all"
        if scope not in SCOPES and can_limit:
            self.scope = "artist"
        self.artist_mbid = artist.mbid if self.scope == "artist" else None

    @property
    def query(self) -> str:
        return release_group_query(self.q, self.mode, self.artist_mbid) if self.q else ""

    def form(self) -> dict[str, str]:
        return {"q": self.q, "mode": self.mode, "scope": self.scope}


def _search_state(request: Request, db: DB, params: SearchParams) -> dict:
    """{'state': idle|not_configured|searching|failed|results, 'hits': [...]} without asking
    MusicBrainz: reading the cache is local, asking is the job."""
    if not params.q:
        return {"state": "idle", "hits": []}
    if not request.app.state.settings.mb_user_agent.strip():
        return {"state": "not_configured", "hits": []}
    hits = peek_release_group_search(db, params.query, svc.SEARCH_LIMIT)
    if hits is not None:
        return {"state": "results", "hits": hits}
    state = job_state(db, search_dedupe_key(params.query))
    return {"state": {"active": "searching", "failed": "failed"}.get(state, "idle"), "hits": []}


def _annotate(db: DB, album: LibraryAlbum, hits: list[ReleaseGroupHit]) -> list[dict]:
    """Search results with whether each is already one of the artist's release groups."""
    mine = {
        rg.mbid: rg.id
        for rg in db.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == album.artist_id))
    }
    return [{"hit": h, "known_id": mine.get(h.data.mbid)} for h in hits]


def _context(
    request: Request,
    db: DB,
    album: LibraryAlbum,
    params: SearchParams,
    show_all: bool = False,
    artist_q: str = "",
) -> dict:
    artist = db.get(Artist, album.artist_id)
    group = db.get(ReleaseGroup, album.release_group_id) if album.release_group_id else None
    candidates = svc.local_candidates(db, album)
    search = _search_state(request, db, params)
    return {
        "album": album,
        "artist": artist,
        "artist_q": artist_q,
        "artist_choices": album_artist.find_artists(db, artist_q, exclude_id=album.artist_id)
        if artist_q.strip()
        else [],
        "group": group,
        "candidates": candidates if show_all else candidates[:LOCAL_SHOWN],
        "candidates_total": len(candidates),
        "show_all": show_all,
        "params": params,
        "state": search["state"],
        "rows": _annotate(db, album, search["hits"]),
        "form_query": urlencode(params.form()),
        "modes": MODES,
        "scopes": SCOPES,
    }


def _album_page(db: DB, album: LibraryAlbum) -> str:
    group = db.get(ReleaseGroup, album.release_group_id) if album.release_group_id else None
    owned = group is not None and album.match_status in ("matched", "tentative", "manual")
    return f"/album/{group.id}" if owned else f"/album/local/{album.id}"


def _page_params(db: DB, album: LibraryAlbum, q: str, mode: str, scope: str) -> SearchParams:
    return SearchParams(db.get(Artist, album.artist_id), q, mode, scope)


@router.get("/{album_id}/match")
def match_page(
    request: Request,
    db: DB,
    album_id: int,
    q: str = "",
    mode: str = "title",
    scope: str = "",
    all: str = "",
    artist_q: str = "",
):
    album = _album_or_404(db, album_id)
    params = _page_params(db, album, q, mode, scope)
    ctx = _context(request, db, album, params, all == "1", artist_q.strip()[:100])
    return render(request, "admin/album_match.html", ctx)


@router.get("/{album_id}/match/results")
def match_results(
    request: Request, db: DB, album_id: int, q: str = "", mode: str = "title", scope: str = ""
):
    """The search results alone; polled while the search job runs."""
    album = _album_or_404(db, album_id)
    params = _page_params(db, album, q, mode, scope)
    return render(request, "admin/_album_match_results.html", _context(request, db, album, params))


@router.post("/{album_id}/match/search")
def match_search(
    request: Request,
    db: DB,
    auth: AdminAuth,
    album_id: int,
    q: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "title",
    scope: Annotated[str, Form()] = "artist",
):
    """Queue the search (a job) and go back to the page, which shows the answer when it is in."""
    album = _album_or_404(db, album_id)
    params = _page_params(db, album, q, mode, scope)
    target = f"/admin/albums/{album.id}/match?{urlencode(params.form())}"
    if params.q and request.app.state.settings.mb_user_agent.strip():
        key = search_dedupe_key(params.query)
        if job_state(db, key) != "active":
            engine.enqueue(
                db,
                "search_release_groups",
                {"query": params.query},
                dedupe_key=key,
                created_by=auth.user.id,
                label=f"{album.title}: {params.q}"[:255],
            )
            db.commit()
            notify_worker(request)
    return RedirectResponse(target, status_code=303)


@router.post("/{album_id}/match")
def match_save(
    request: Request,
    db: DB,
    auth: AdminAuth,
    album_id: int,
    choice: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "title",
    scope: Annotated[str, Form()] = "artist",
):
    """The decision: a release group of the artist, a search result ("mbid:<id>"), "none" (not on
    MusicBrainz) or "auto" (undo)."""
    album = _album_or_404(db, album_id)
    artist = db.get(Artist, album.artist_id)
    params = _page_params(db, album, q, mode, scope)
    try:
        if choice.startswith("mbid:"):
            hit = next(
                (
                    h
                    for h in peek_release_group_search(db, params.query, svc.SEARCH_LIMIT) or []
                    if h.data.mbid == choice[5:]
                ),
                None,
            )
            if hit is None:  # the cached answer is gone: the choice cannot be trusted
                raise svc.MatchError("search_expired")
            choice = str(svc.adopt_hit(db, album.artist_id, hit).id)
        svc.apply_choice(db, album, auth.user, choice, artist_name=artist.name if artist else "")
    except svc.MatchError as exc:
        db.rollback()
        ctx = _context(request, db, album, params)
        ctx["error"] = f"error.{exc.code}"
        return render(request, "admin/album_match.html", ctx, status_code=400)
    db.commit()
    return RedirectResponse(f"{_album_page(db, album)}?flash=match_saved", status_code=303)


@router.post("/{album_id}/artist")
def artist_save(
    request: Request,
    db: DB,
    auth: AdminAuth,
    album_id: int,
    artist_id: Annotated[str, Form()] = "",
    new_name: Annotated[str, Form()] = "",
):
    """Give the album to another artist: one that exists, or a new one by name."""
    album = _album_or_404(db, album_id)
    chosen = int(artist_id) if artist_id.isdigit() and artist_id.isascii() else None
    try:
        target = album_artist.reassign(
            db, album, auth.user, artist_id=chosen, new_name="" if chosen else new_name
        )
    except album_artist.ArtistChoiceError as exc:
        db.rollback()
        params = _page_params(db, album, "", "title", "")
        ctx = _context(request, db, album, params)
        ctx["error"] = f"error.{exc.code}"
        return render(request, "admin/album_match.html", ctx, status_code=400)
    db.commit()
    if request.app.state.settings.mb_user_agent.strip() and target.metadata_synced_at is None:
        engine.enqueue(  # a new artist gets its MusicBrainz identity
            db,
            "sync_pending_artists",
            {},
            dedupe_key="sync_pending_artists",
            created_by=auth.user.id,
        )
        notify_worker(request)
    return RedirectResponse(f"/admin/albums/{album.id}/match?flash=artist_moved", status_code=303)


# ------------------------------------------------------------------- comparison


def _compare_context(
    request: Request, db: DB, auth, album: LibraryAlbum, group_id: str, *, start: bool
) -> dict:
    """Your album against one release group of the artist. The tracks of the group come from
    MusicBrainz, so when they are not stored yet the page shows "loading" and the job (queued
    by the POST, never by a GET) fills them in."""
    group = db.get(ReleaseGroup, int(group_id)) if group_id.isdigit() else None
    if group is None or group.artist_id != album.artist_id:
        raise HTTPException(status_code=404, detail="not_found")
    ctx: dict = {"album": album, "group": group, "state": "ready", "cmp": None}
    if ":" in group.mbid:  # a fallback provider's album: MusicBrainz has no track list for it
        ctx["state"] = "unavailable"
        return ctx
    if db.get(TracklistFetch, group.id) is None:
        if not request.app.state.settings.mb_user_agent.strip():
            ctx["state"] = "not_configured"
            return ctx
        key = tracklist_dedupe_key(group.id)
        state = job_state(db, key)
        if state == "failed" and not start:
            ctx["state"] = "failed"
            return ctx
        if state != "active":
            if not start:
                ctx["state"] = "idle"
                return ctx
            engine.enqueue(
                db,
                "fetch_tracklist",
                {"release_group_id": group.id},
                dedupe_key=key,
                created_by=auth.user.id,
            )
            db.commit()
            notify_worker(request)
        ctx["state"] = "loading"
        return ctx
    local = list(
        db.scalars(
            select(LibraryTrack)
            .where(LibraryTrack.album_id == album.id)
            .order_by(LibraryTrack.disc_no, LibraryTrack.track_no, LibraryTrack.title)
        )
    )
    ctx["cmp"] = compare_svc.compare(album, local, group, load_tracks(db, group.id))
    return ctx


@router.get("/{album_id}/match/compare")
def match_compare(request: Request, db: DB, auth: AdminAuth, album_id: int, rg: str = ""):
    """The comparison alone; polled while the track list is being fetched."""
    album = _album_or_404(db, album_id)
    ctx = _compare_context(request, db, auth, album, rg, start=False)
    return render(request, "admin/_album_compare.html", ctx)


@router.post("/{album_id}/match/compare")
def match_compare_start(
    request: Request, db: DB, auth: AdminAuth, album_id: int, rg: Annotated[str, Form()] = ""
):
    album = _album_or_404(db, album_id)
    ctx = _compare_context(request, db, auth, album, rg, start=True)
    return render(request, "admin/_album_compare.html", ctx)
