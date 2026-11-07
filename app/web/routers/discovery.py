"""Discovery: global artist search, similar artists and album previews.

Everything that asks an outside service runs as a job. These pages only read what the jobs
left in the database (or in `api_cache`) and, when something is missing, queue the job and
poll until it is done.
"""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth.deps import DB, UserAuth
from app.integrations.musicbrainz_client import ArtistHit, peek_artist_search
from app.jobs import engine
from app.jobs.handlers.fetch_similar import similar_dedupe_key
from app.jobs.handlers.resolve_previews import previews_dedupe_key
from app.jobs.handlers.search_artists import SEARCH_LIMIT, search_dedupe_key
from app.jobs.handlers.sync_artist import sync_dedupe_key
from app.models import (
    ACTIVE_STATUSES,
    STATUS_FAILED,
    Artist,
    Job,
    PreviewLookup,
    ReleaseGroup,
    SimilarArtist,
    utcnow,
)
from app.services.discovery import (
    clean_query,
    list_similar,
    open_external_artist,
    similar_is_fresh,
    valid_mbid,
)
from app.services.previews import preview_state
from app.web.routers.jobs import notify_worker
from app.web.templating import render

router = APIRouter()

FAILURE_MEMORY = timedelta(minutes=10)  # a failed job is reported, not retried in a loop
MIN_QUERY = 2


def _job_state(db: DB, key: str) -> str:
    """'active', 'failed' (recently) or 'none' for the job with this dedupe key."""
    if db.scalar(select(Job.id).where(Job.dedupe_key == key, Job.status.in_(ACTIVE_STATUSES))):
        return "active"
    latest = db.scalar(select(Job).where(Job.dedupe_key == key).order_by(Job.id.desc()).limit(1))
    if (
        latest
        and latest.status == STATUS_FAILED
        and (latest.finished_at or utcnow()) > (utcnow() - FAILURE_MEMORY)
    ):
        return "failed"
    return "none"


def _queue(request: Request, db: DB, auth: UserAuth, type_: str, payload: dict, key: str) -> None:
    engine.enqueue(db, type_, payload, dedupe_key=key, created_by=auth.user.id)
    notify_worker(request)


# ---------------------------------------------------------------------- search


def _annotate(db: DB, hits: list[ArtistHit]) -> list[dict]:
    """Search results with the artist row we already have for them, if any."""
    known = {
        a.mbid: a
        for a in db.scalars(select(Artist).where(Artist.mbid.in_([h.mbid for h in hits])))
        if a.mbid
    }
    return [
        {
            "hit": h,
            "artist": known.get(h.mbid),
            "in_library": bool(known.get(h.mbid) and known[h.mbid].in_library),
        }
        for h in hits
    ]


def _search_context(
    request: Request, db: DB, auth: UserAuth, raw: str, *, retry: bool = False
) -> dict:
    query = clean_query(raw)
    ctx: dict = {"q": query, "state": "empty", "rows": []}
    if len(query) < MIN_QUERY:
        return ctx
    if not request.app.state.settings.mb_user_agent.strip():
        ctx["state"] = "not_configured"
        return ctx
    hits = peek_artist_search(db, query, SEARCH_LIMIT)
    if hits is not None:
        ctx.update(state="results", rows=_annotate(db, hits))
        return ctx
    key = search_dedupe_key(query)
    state = _job_state(db, key)
    if state == "failed" and not retry:
        ctx["state"] = "failed"
        return ctx
    if state != "active":
        _queue(request, db, auth, "search_artists", {"query": query}, key)
    ctx["state"] = "searching"
    return ctx


@router.get("/search")
def search_page(request: Request, db: DB, auth: UserAuth, q: str = "", retry: str = ""):
    ctx = _search_context(request, db, auth, q, retry=retry == "1")
    return render(request, "search.html", ctx)


@router.get("/search/results")
def search_results(request: Request, db: DB, auth: UserAuth, q: str = ""):
    """The results alone; polled while the search job runs."""
    return render(request, "_search_results.html", _search_context(request, db, auth, q))


def _open_artist(request: Request, db: DB, auth: UserAuth, artist: Artist, created: bool):
    if created or artist.metadata_synced_at is None:
        db.commit()
        _queue(
            request,
            db,
            auth,
            "sync_artist_metadata",
            {"artist_id": artist.id},
            sync_dedupe_key(artist.id),
        )
        _queue(
            request,
            db,
            auth,
            "fetch_similar_artists",
            {"artist_id": artist.id},
            similar_dedupe_key(artist.id),
        )
    return RedirectResponse(f"/artist/{artist.id}", status_code=303)


@router.post("/search/open")
def search_open(
    request: Request,
    db: DB,
    auth: UserAuth,
    q: Annotated[str, Form()] = "",
    mbid: Annotated[str, Form()] = "",
):
    """Open an artist found by a search. Name and details come from the cached search result,
    never from the form."""
    query = clean_query(q)
    hits = peek_artist_search(db, query, SEARCH_LIMIT) if valid_mbid(mbid) else None
    hit = next((h for h in hits or [] if h.mbid == mbid.lower()), None)
    if hit is None:  # the cached answer is gone: search again
        return RedirectResponse(f"/search?q={query}", status_code=303)
    artist, created = open_external_artist(
        db, hit.mbid, hit.name, country=hit.country, disambiguation=hit.disambiguation
    )
    return _open_artist(request, db, auth, artist, created)


# ------------------------------------------------------------ similar artists


def _similar_context(request: Request, db: DB, auth: UserAuth, artist: Artist, *, retry=False):
    settings = request.app.state.settings
    ctx: dict = {"artist": artist, "items": [], "state": "ready"}
    items = list_similar(db, artist.id)
    key = similar_dedupe_key(artist.id)
    if items:
        ctx["items"] = items
        if not similar_is_fresh(db, artist.id) and _job_state(db, key) == "none":
            _queue(request, db, auth, "fetch_similar_artists", {"artist_id": artist.id}, key)
        return ctx
    if similar_is_fresh(db, artist.id):
        return ctx  # looked up, nothing found
    can_lastfm = bool(settings.lastfm_api_key.strip())
    can_lb = bool(settings.mb_user_agent.strip() and artist.mbid)
    if not (can_lastfm or can_lb):
        ctx["state"] = "unavailable"
        return ctx
    state = _job_state(db, key)
    if state == "failed" and not retry:
        ctx["state"] = "failed"
        return ctx
    if state != "active":
        _queue(request, db, auth, "fetch_similar_artists", {"artist_id": artist.id}, key)
    ctx["state"] = "loading"
    return ctx


def _artist_or_404(db: DB, artist_id: int) -> Artist:
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(status_code=404, detail="not_found")
    return artist


@router.post("/artist/{artist_id}/similar")
def similar_load(request: Request, db: DB, auth: UserAuth, artist_id: int, retry: str = ""):
    """Called when the artist page opens (and by the retry button): queues the job if needed."""
    artist = _artist_or_404(db, artist_id)
    ctx = _similar_context(request, db, auth, artist, retry=retry == "1")
    return render(request, "_similar.html", ctx)


@router.get("/artist/{artist_id}/similar")
def similar_poll(request: Request, db: DB, auth: UserAuth, artist_id: int):
    artist = _artist_or_404(db, artist_id)
    return render(request, "_similar.html", _similar_context(request, db, auth, artist))


@router.post("/artist/{artist_id}/similar/open")
def similar_open(
    request: Request,
    db: DB,
    auth: UserAuth,
    artist_id: int,
    mbid: Annotated[str, Form()] = "",
):
    """Open an artist from the similar list (name taken from the stored list)."""
    _artist_or_404(db, artist_id)
    row = (
        db.scalar(
            select(SimilarArtist).where(
                SimilarArtist.artist_id == artist_id, SimilarArtist.mbid == mbid.lower()
            )
        )
        if valid_mbid(mbid)
        else None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    artist, created = open_external_artist(db, row.mbid, row.name)
    return _open_artist(request, db, auth, artist, created)


# -------------------------------------------------------------------- previews


def _group_or_404(db: DB, group_id: int) -> ReleaseGroup:
    group = db.get(ReleaseGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="not_found")
    return group


def _preview_context(
    request: Request, db: DB, auth: UserAuth, group: ReleaseGroup, *, enqueue: bool, retry=False
) -> dict:
    ctx: dict = {"g": group, "state": "loading", "tracks": [], "lookup": None}
    state = preview_state(db, group.id)
    if state.state == "ready":
        ctx.update(
            state="ready",
            lookup=db.get(PreviewLookup, group.id),
            tracks=[
                {"title": t.title, "url": t.url, "n": t.position, "disc": t.disc}
                for t in state.tracks or []
            ],
        )
        return ctx
    if state.state == "missing":
        ctx["state"] = "missing"
        return ctx
    key = previews_dedupe_key(group.id)
    job = _job_state(db, key)
    if job == "failed" and not retry:
        ctx["state"] = "failed"
    elif job != "active" and enqueue:
        _queue(
            request,
            db,
            auth,
            "resolve_previews",
            {"release_group_id": group.id, "force": state.state == "stale"},
            key,
        )
    return ctx


@router.post("/previews/{group_id}")
def preview_load(request: Request, db: DB, auth: UserAuth, group_id: int, retry: str = ""):
    group = _group_or_404(db, group_id)
    ctx = _preview_context(request, db, auth, group, enqueue=True, retry=retry == "1")
    return render(request, "_preview.html", ctx)


@router.get("/previews/{group_id}")
def preview_poll(request: Request, db: DB, auth: UserAuth, group_id: int):
    group = _group_or_404(db, group_id)
    return render(
        request, "_preview.html", _preview_context(request, db, auth, group, enqueue=False)
    )
