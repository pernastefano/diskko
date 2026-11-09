"""Discovery: global artist search, similar artists and album previews.

Everything that asks an outside service runs as a job. These pages only read what the jobs
left in the database (or in `api_cache`) and, when something is missing, queue the job and
poll until it is done.
"""

from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.auth.deps import DB, UserAuth
from app.integrations.musicbrainz_client import (
    ArtistHit,
    ReleaseGroupHit,
    peek_artist_search,
    peek_release_group_search,
    release_group_query,
)
from app.jobs import engine
from app.jobs.handlers.fetch_similar import similar_dedupe_key
from app.jobs.handlers.search_artists import SEARCH_LIMIT, search_dedupe_key
from app.jobs.handlers.search_release_groups import SEARCH_LIMIT as ALBUM_LIMIT
from app.jobs.handlers.search_release_groups import search_dedupe_key as album_dedupe_key
from app.jobs.handlers.sync_artist import sync_dedupe_key
from app.models import (
    ACTIVE_STATUSES,
    STATUS_FAILED,
    Artist,
    Job,
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
from app.services.library_views import list_albums, list_artists
from app.services.musicbrainz.missing import get_missing_types
from app.web.routers.jobs import notify_worker
from app.web.templating import render

router = APIRouter()

FAILURE_MEMORY = timedelta(minutes=10)  # a failed job is reported, not retried in a loop
MIN_QUERY = 2


def job_state(db: DB, key: str) -> str:
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


def queue_job(
    request: Request, db: DB, auth: UserAuth, type_: str, payload: dict, key: str
) -> None:
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


MAX_ACTIVE_SEARCHES = 3  # per user and kind of search: one person cannot fill the MusicBrainz queue


def _search_context(
    request: Request, db: DB, auth: UserAuth, raw: str, *, retry: bool = False, start: bool = False
) -> dict:
    """What the search page shows. Looking never asks MusicBrainz: when there is no answer yet
    the state is "starting", and the page then POSTs /search/start (`start=True`) to queue the
    job. A GET that queued jobs could be triggered from any other page by a link or an image."""
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
    state = job_state(db, key)
    if state == "failed" and not retry:
        ctx["state"] = "failed"
        return ctx
    if state == "active":
        ctx["state"] = "searching"
    elif not start:
        ctx.update(state="starting", retry=retry)
    elif _active_searches(db, auth.user.id) >= MAX_ACTIVE_SEARCHES:
        ctx["state"] = "busy"
    else:
        queue_job(request, db, auth, "search_artists", {"query": query}, key)
        ctx["state"] = "searching"
    return ctx


def _active_searches(db: DB, user_id: int, type_: str = "search_artists") -> int:
    return (
        db.scalar(
            select(func.count(Job.id)).where(
                Job.type == type_,
                Job.created_by == user_id,
                Job.status.in_(ACTIVE_STATUSES),
            )
        )
        or 0
    )


def _annotate_albums(db: DB, hits: list[ReleaseGroupHit]) -> list[dict]:
    """Album results with the artist row we already have for them, if any."""
    mbids = [h.artist_mbid for h in hits if h.artist_mbid]
    known = {a.mbid: a for a in db.scalars(select(Artist).where(Artist.mbid.in_(mbids))) if a.mbid}
    return [{"hit": h, "artist": known.get(h.artist_mbid)} for h in hits]


def _album_search_context(
    request: Request, db: DB, auth: UserAuth, raw: str, *, retry: bool = False, start: bool = False
) -> dict:
    """The album half of the search: what MusicBrainz has as albums for the text (the words in the
    title or in the artist's name; a Lucene query such as `tag:dance` goes through as typed).
    Same rules as the artist half: a GET only reads the cache, the POST queues the job."""
    query = clean_query(raw)
    ctx: dict = {"q": query, "state": "empty", "rows": []}
    if len(query) < MIN_QUERY:
        return ctx
    if not request.app.state.settings.mb_user_agent.strip():
        ctx["state"] = "not_configured"
        return ctx
    search = release_group_query(query, "anything")
    hits = peek_release_group_search(db, search, ALBUM_LIMIT)
    if hits is not None:
        ctx.update(state="results", rows=_annotate_albums(db, hits))
        return ctx
    key = album_dedupe_key(search)
    state = job_state(db, key)
    if state == "failed" and not retry:
        ctx["state"] = "failed"
    elif state == "active":
        ctx["state"] = "searching"
    elif not start:
        ctx.update(state="starting", retry=retry)
    elif _active_searches(db, auth.user.id, "search_release_groups") >= MAX_ACTIVE_SEARCHES:
        ctx["state"] = "busy"
    else:
        queue_job(request, db, auth, "search_release_groups", {"query": search}, key)
        ctx["state"] = "searching"
    return ctx


@router.post("/search/albums/start")
def search_albums_start(request: Request, db: DB, auth: UserAuth, q: str = "", retry: str = ""):
    ctx = _album_search_context(request, db, auth, q, retry=retry == "1", start=True)
    return render(request, "_search_albums.html", ctx)


@router.get("/search/albums/results")
def search_albums_results(request: Request, db: DB, auth: UserAuth, q: str = ""):
    """The album results alone; polled while the search job runs."""
    return render(request, "_search_albums.html", _album_search_context(request, db, auth, q))


@router.post("/search/open-album")
def search_open_album(
    request: Request,
    db: DB,
    auth: UserAuth,
    q: Annotated[str, Form()] = "",
    mbid: Annotated[str, Form()] = "",
):
    """Open the artist of an album found by a search (the album is in the artist's discography).
    Names come from the cached search result, never from the form."""
    query = clean_query(q)
    search = release_group_query(query, "anything") if len(query) >= MIN_QUERY else ""
    hits = (
        peek_release_group_search(db, search, ALBUM_LIMIT) if search and valid_mbid(mbid) else None
    )
    hit = next((h for h in hits or [] if h.data.mbid == mbid.lower()), None)
    if hit is None or not hit.artist_mbid or not hit.artist_name:
        return RedirectResponse(f"/search?{urlencode({'q': query})}", status_code=303)
    artist, created = open_external_artist(db, hit.artist_mbid, hit.artist_name)
    return _open_artist(request, db, auth, artist, created)


LOCAL_SHOWN = 8  # artists and albums of the library listed above the MusicBrainz results


def _library_matches(request: Request, db: DB, query: str) -> dict:
    """What the library itself has for the text: its artists, and its albums (by title, by artist
    or by year). Local and quick, so it is part of the page, not a job."""
    if len(query) < MIN_QUERY:
        return {
            "local_artists": [],
            "local_albums": [],
            "local_artist_total": 0,
            "local_album_total": 0,
        }
    artists, artist_total = list_artists(
        db, types=get_missing_types(db), q=query, page_size=LOCAL_SHOWN
    )
    albums, album_total = list_albums(
        db, request.app.state.quality_profiles, q=query, page_size=LOCAL_SHOWN
    )
    return {
        "local_artists": artists,
        "local_albums": albums,
        "local_artist_total": artist_total,
        "local_album_total": album_total,
    }


@router.get("/search")
def search_page(request: Request, db: DB, auth: UserAuth, q: str = "", retry: str = ""):
    ctx = _search_context(request, db, auth, q, retry=retry == "1")
    ctx.update(_library_matches(request, db, ctx["q"]))
    ctx["albums"] = _album_search_context(request, db, auth, q, retry=retry == "1")
    return render(request, "search.html", ctx)


@router.post("/search/start")
def search_start(request: Request, db: DB, auth: UserAuth, q: str = "", retry: str = ""):
    """Queue the search (the page asks for it by itself) and answer with the results block."""
    ctx = _search_context(request, db, auth, q, retry=retry == "1", start=True)
    return render(request, "_search_results.html", ctx)


@router.get("/search/results")
def search_results(request: Request, db: DB, auth: UserAuth, q: str = ""):
    """The results alone; polled while the search job runs."""
    return render(request, "_search_results.html", _search_context(request, db, auth, q))


def _open_artist(request: Request, db: DB, auth: UserAuth, artist: Artist, created: bool):
    if created or artist.metadata_synced_at is None:
        db.commit()
        queue_job(
            request,
            db,
            auth,
            "sync_artist_metadata",
            {"artist_id": artist.id},
            sync_dedupe_key(artist.id),
        )
        queue_job(
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
        return RedirectResponse(f"/search?{urlencode({'q': query})}", status_code=303)
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
        if not similar_is_fresh(db, artist.id) and job_state(db, key) == "none":
            queue_job(request, db, auth, "fetch_similar_artists", {"artist_id": artist.id}, key)
        return ctx
    if similar_is_fresh(db, artist.id):
        return ctx  # looked up, nothing found
    can_lastfm = bool(settings.lastfm_api_key.strip())
    can_lb = bool(settings.mb_user_agent.strip() and artist.mbid)
    if not (can_lastfm or can_lb):
        ctx["state"] = "unavailable"
        return ctx
    state = job_state(db, key)
    if state == "failed" and not retry:
        ctx["state"] = "failed"
        return ctx
    if state != "active":
        queue_job(request, db, auth, "fetch_similar_artists", {"artist_id": artist.id}, key)
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
