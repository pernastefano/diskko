"""The artist page's profile fragments: the short biography and the top tracks.

Like the similar artists, they only read what the jobs left in the database; when something is
missing or old they queue the job and the fragment polls until it is done.
"""

from fastapi import APIRouter, HTTPException, Request

from app.auth.deps import DB, UserAuth
from app.integrations.providers import provider_order
from app.jobs.handlers.fetch_artist_profile import profile_dedupe_key
from app.jobs.handlers.fetch_top_tracks import top_dedupe_key
from app.models import Artist
from app.services.artist_profile import (
    bio_is_fresh,
    get_bio,
    list_top_tracks,
    preview_alive,
    short_bio,
    top_state,
)
from app.web.routers.discovery import job_state, queue_job
from app.web.templating import current_lang, render

router = APIRouter()


def _artist_or_404(db: DB, artist_id: int) -> Artist:
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(status_code=404, detail="not_found")
    return artist


# ------------------------------------------------------------------ biography


def _bio_context(request: Request, db: DB, auth: UserAuth, artist: Artist, *, retry=False):
    ctx: dict = {"artist": artist, "state": "ready", "bio": None, "short": ""}
    row = get_bio(db, artist.id, current_lang(request))
    key = profile_dedupe_key(artist.id)
    state = job_state(db, key)
    if not bio_is_fresh(artist) and (state == "none" or (state == "failed" and retry)):
        queue_job(request, db, auth, "fetch_artist_profile", {"artist_id": artist.id}, key)
        state = "active"
    if row is not None:
        ctx.update(bio=row, short=short_bio(row.text))
    elif state == "active" or not bio_is_fresh(artist):
        ctx["state"] = "loading" if state == "active" else "none"
    return ctx


@router.post("/artist/{artist_id}/bio")
def bio_load(request: Request, db: DB, auth: UserAuth, artist_id: int):
    artist = _artist_or_404(db, artist_id)
    return render(request, "_artist_bio.html", _bio_context(request, db, auth, artist))


@router.get("/artist/{artist_id}/bio")
def bio_poll(request: Request, db: DB, auth: UserAuth, artist_id: int):
    artist = _artist_or_404(db, artist_id)
    return render(request, "_artist_bio.html", _bio_context(request, db, auth, artist))


# ------------------------------------------------------------------ top tracks


def _top_context(request: Request, db: DB, auth: UserAuth, artist: Artist, *, retry=False):
    settings = request.app.state.settings
    ctx: dict = {"artist": artist, "state": "ready", "tracks": [], "alive": set()}
    order = provider_order(settings, "toptracks")
    usable = "deezer" in order or ("lastfm" in order and settings.lastfm_api_key.strip())
    tracks = list_top_tracks(db, artist.id)
    ctx["tracks"] = tracks
    ctx["alive"] = {t.id for t in tracks if preview_alive(t)}
    ctx["providers"] = {t.provider for t in tracks} | {t.preview_provider for t in tracks}
    freshness = top_state(artist, tracks)
    if freshness == "ready":
        return ctx
    if not usable:
        ctx["state"] = "unavailable" if not tracks else "ready"
        return ctx
    key = top_dedupe_key(artist.id)
    state = job_state(db, key)
    if state == "failed" and not retry:
        ctx["state"] = "failed" if not tracks else "ready"
        return ctx
    if state != "active":
        queue_job(request, db, auth, "fetch_top_tracks", {"artist_id": artist.id}, key)
    ctx["state"] = "loading"
    return ctx


@router.post("/artist/{artist_id}/top")
def top_load(request: Request, db: DB, auth: UserAuth, artist_id: int, retry: str = ""):
    """Called when the page opens (and by the retry / renew buttons)."""
    artist = _artist_or_404(db, artist_id)
    ctx = _top_context(request, db, auth, artist, retry=retry == "1")
    return render(request, "_artist_top.html", ctx)


@router.get("/artist/{artist_id}/top")
def top_poll(request: Request, db: DB, auth: UserAuth, artist_id: int):
    artist = _artist_or_404(db, artist_id)
    return render(request, "_artist_top.html", _top_context(request, db, auth, artist))
