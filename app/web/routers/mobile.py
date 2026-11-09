"""The mobile interface (`/m`): a simplified, installable (PWA) view of DISKKO.

It shows artists and albums, searches for new artists, and makes and follows download requests.
Everything that is best done on a desktop (jobs, status, settings, matching, re-tagging, the
library management) is not here. The pages only compose what the web interface already has: the
same read models and the same actions (the request, the choice of a candidate, cancel, retry);
the action routes call the web ones and send the browser back to the mobile page.
"""

import math
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import func, select

from app.auth.deps import DB, SettingsDep, UserAuth
from app.auth.users import UserError
from app.i18n import LANG_NAMES
from app.models import THEMES, Artist, DownloadRequest, LibraryAlbum, ReleaseGroup
from app.services import downloads as svc
from app.services.app_settings import get_setting
from app.services.downloads import USER_QUALITY_KEY
from app.services.library_views import PAGE_SIZE, list_albums, list_artists
from app.services.musicbrainz.missing import get_missing_types
from app.web.routers import albums as albums_web
from app.web.routers import artist_profile as profile_web
from app.web.routers import auth as auth_web
from app.web.routers import discovery as discovery_web
from app.web.routers import library as library_web
from app.web.routers import preferences as preferences_web
from app.web.routers import requests as requests_web
from app.web.templating import current_lang, render

router = APIRouter(prefix="/m")

STATIC_M = Path(__file__).resolve().parent.parent / "static" / "m"
REQUEST_FILTERS = ("all", "active", "finished", "failed")
REQUEST_PAGE = 20


def page(request: Request, template: str, ctx: dict, tab: str, status_code: int = 200):
    return render(request, f"m/{template}", {**ctx, "tab": tab}, status_code=status_code)


_WEB_PAGE = re.compile(r"^/(requests|artist|album|search)(?=[/?]|$)")


def to_mobile(response):
    """The web action sent the browser to a web page: send it to the mobile page instead."""
    location = response.headers.get("location", "")
    response.headers["location"] = _WEB_PAGE.sub(r"/m/\1", location, count=1)
    return response


# ----------------------------------------------------------------- the app itself


@router.get("")
@router.get("/")
def home(auth: UserAuth):
    return RedirectResponse("/m/artists", status_code=303)


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    """The service worker: served from /m so its scope is the mobile pages. No sign-in needed."""
    return FileResponse(
        STATIC_M / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/m/"},
    )


OFFLINE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>DISKKO</title>
<style>body{font-family:system-ui,sans-serif;background:#181b1f;color:#e8e6e1;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0;text-align:center;padding:1rem}
a{color:#e0a847}</style></head><body><div><h1>DISKKO</h1>
<p>You are offline / Sei offline.</p><p><a href="/m/">Retry / Riprova</a></p></div></body></html>"""


@router.get("/offline", include_in_schema=False)
def offline():
    return Response(OFFLINE, media_type="text/html")


# ---------------------------------------------------------------------- artists


def _list_ctx(total: int, page_no: int, query: dict, path: str) -> dict:
    pages = max(math.ceil(total / PAGE_SIZE), 1)
    more = f"{path}?{urlencode({**query, 'page_no': page_no + 1})}" if page_no < pages else None
    return {"total": total, "next_url": more}


@router.get("/artists")
def artists(request: Request, db: DB, auth: UserAuth, q: str = "", page_no: int = 1):
    return _artists(request, db, q, page_no, "artists.html")


@router.get("/artists/items")
def artists_items(request: Request, db: DB, auth: UserAuth, q: str = "", page_no: int = 1):
    return _artists(request, db, q, page_no, "_artist_items.html")


def _artists(request: Request, db: DB, q: str, page_no: int, template: str):
    q = q.strip()[:100]
    page_no = max(page_no, 1)
    rows, total = list_artists(db, types=get_missing_types(db), q=q, page=page_no)
    ctx = {
        "rows": rows,
        "q": q,
        **_list_ctx(total, page_no, {"q": q} if q else {}, "/m/artists/items"),
    }
    return page(request, template, ctx, "artists")


@router.get("/albums")
def albums(request: Request, db: DB, auth: UserAuth, q: str = "", page_no: int = 1):
    return _albums(request, db, q, page_no, "albums.html")


@router.get("/albums/items")
def albums_items(request: Request, db: DB, auth: UserAuth, q: str = "", page_no: int = 1):
    return _albums(request, db, q, page_no, "_album_items.html")


def _albums(request: Request, db: DB, q: str, page_no: int, template: str):
    q = q.strip()[:100]
    page_no = max(page_no, 1)
    rows, total = list_albums(db, request.app.state.quality_profiles, q=q, page=page_no)
    ctx = {
        "rows": rows,
        "q": q,
        **_list_ctx(total, page_no, {"q": q} if q else {}, "/m/albums/items"),
    }
    return page(request, template, ctx, "albums")


@router.get("/artist/{artist_id}")
def artist(request: Request, db: DB, auth: UserAuth, artist_id: int, all: str = ""):
    row = library_web._artist_or_404(db, artist_id)
    ctx = library_web._artist_context(db, auth, row, all == "1", request.app.state.settings)
    library_web._ensure_covers(request, db, auth, row, ctx["sections"], ctx["types"])
    ctx["covers_active"] = library_web._cover_job_active(db, artist_id)
    ctx["syncing"] = library_web._sync_active(db, artist_id)
    return page(request, "artist.html", ctx, "artists")


@router.get("/artist/{artist_id}/sections")
def artist_sections(request: Request, db: DB, auth: UserAuth, artist_id: int, all: str = ""):
    row = library_web._artist_or_404(db, artist_id)
    ctx = library_web._artist_context(db, auth, row, all == "1", request.app.state.settings)
    ctx["covers_active"] = library_web._cover_job_active(db, artist_id)
    ctx["syncing"] = library_web._sync_active(db, artist_id)
    return page(request, "_artist_sections.html", ctx, "artists")


@router.post("/artist/{artist_id}/top")
@router.get("/artist/{artist_id}/top")
def artist_top(request: Request, db: DB, auth: UserAuth, artist_id: int, retry: str = ""):
    """POST when the page opens (queues the job if needed), GET while it is polled."""
    row = profile_web._artist_or_404(db, artist_id)
    ctx = profile_web._top_context(request, db, auth, row, retry=retry == "1")
    return page(request, "_top.html", ctx, "artists")


@router.post("/artist/{artist_id}/similar")
@router.get("/artist/{artist_id}/similar")
def artist_similar(request: Request, db: DB, auth: UserAuth, artist_id: int, retry: str = ""):
    row = discovery_web._artist_or_404(db, artist_id)
    ctx = discovery_web._similar_context(request, db, auth, row, retry=retry == "1")
    return page(request, "_similar.html", ctx, "artists")


@router.post("/artist/{artist_id}/similar/open")
def similar_open(
    request: Request, db: DB, auth: UserAuth, artist_id: int, mbid: Annotated[str, Form()] = ""
):
    return to_mobile(discovery_web.similar_open(request, db, auth, artist_id, mbid))


# ----------------------------------------------------------------------- albums


def _request_ctx(db: DB, artist_row: Artist, group: ReleaseGroup, unavailable: bool = False):
    info = svc.request_infos(db, [group.id]).get(group.id)
    return {
        "artist": artist_row,
        "g": group,
        "status": info.status if info else None,
        "info": info,
        "unavailable": unavailable,
    }


@router.post("/request/{artist_id}/{group_id}")
def request_album(request: Request, db: DB, auth: UserAuth, artist_id: int, group_id: int):
    """Ask for an album (the same request as on the web page); answers with the button."""
    row = library_web._artist_or_404(db, artist_id)
    group = db.get(ReleaseGroup, group_id)
    if group is None or group.artist_id != artist_id:
        raise HTTPException(status_code=404, detail="not_found")
    ok = library_web.queue_download(request, db, auth, row, group)
    return page(request, "_request_button.html", _request_ctx(db, row, group, not ok), "artists")


@router.get("/requests/{request_id}/control")
def request_control(request: Request, db: DB, auth: UserAuth, request_id: int):
    """The button of an album, refreshed while its request runs."""
    req = requests_web._get(db, auth, request_id)
    group = db.get(ReleaseGroup, req.release_group_id)
    ctx = _request_ctx(db, db.get(Artist, group.artist_id), group)
    return page(request, "_request_button.html", ctx, "requests")


def _album_page(request: Request, db: DB, auth: UserAuth, group: ReleaseGroup):
    ctx = albums_web._group_context(request, db, auth, group, enqueue=False)
    return page(request, "album.html", ctx, "albums")


@router.get("/album/{group_id}")
def album(request: Request, db: DB, auth: UserAuth, group_id: int):
    group = db.get(ReleaseGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _album_page(request, db, auth, group)


@router.post("/album/{group_id}/tracks")
@router.get("/album/{group_id}/tracks")
def album_tracks(request: Request, db: DB, auth: UserAuth, group_id: int, refresh: str = ""):
    """POST when the page opens (queues what is missing), GET while it is polled."""
    group = db.get(ReleaseGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="not_found")
    ctx = albums_web._group_context(
        request, db, auth, group, enqueue=request.method == "POST", refresh=refresh == "1"
    )
    return page(request, "_tracks.html", ctx, "albums")


@router.get("/album/local/{album_id}")
def local_album(request: Request, db: DB, auth: UserAuth, album_id: int):
    """An album of the library that MusicBrainz could not be matched to: its tracks."""
    row = db.get(LibraryAlbum, album_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    if row.release_group_id and row.match_status in ("matched", "tentative", "manual"):
        return RedirectResponse(f"/m/album/{row.release_group_id}", status_code=303)
    owner = db.get(Artist, row.artist_id)
    ctx = albums_web._base_context(None, row, owner)
    rows = albums_web.owned_rows(db, row)
    albums_web.attach_artists(db, rows, owner)
    ctx.update(mode="local", rows=rows, loading=False, lookup=None, tracks_known=True)
    ctx["summary"] = albums_web._summary(rows)
    ctx["player"] = albums_web.player_data(rows, album=row.title, artist=owner.name)
    return page(request, "album.html", ctx, "albums")


# ----------------------------------------------------------------------- search


@router.get("/search")
def search(request: Request, db: DB, auth: UserAuth, q: str = "", retry: str = ""):
    ctx = discovery_web._search_context(request, db, auth, q, retry=retry == "1")
    ctx.update(discovery_web._library_matches(request, db, ctx["q"]))
    return page(request, "search.html", ctx, "search")


@router.post("/search/start")
def search_start(request: Request, db: DB, auth: UserAuth, q: str = "", retry: str = ""):
    ctx = discovery_web._search_context(request, db, auth, q, retry=retry == "1", start=True)
    return page(request, "_search_results.html", ctx, "search")


@router.get("/search/results")
def search_results(request: Request, db: DB, auth: UserAuth, q: str = ""):
    ctx = discovery_web._search_context(request, db, auth, q)
    return page(request, "_search_results.html", ctx, "search")


@router.post("/search/open")
def search_open(
    request: Request,
    db: DB,
    auth: UserAuth,
    q: Annotated[str, Form()] = "",
    mbid: Annotated[str, Form()] = "",
):
    return to_mobile(discovery_web.search_open(request, db, auth, q, mbid))


# --------------------------------------------------------------------- requests


def _request_list(db: DB, auth: UserAuth, status: str, page_no: int) -> dict:
    status = status if status in REQUEST_FILTERS else "all"
    stmt = select(DownloadRequest)
    if not auth.user.is_admin:
        stmt = stmt.where(DownloadRequest.user_id == auth.user.id)
    wanted = requests_web.FILTERS[status]
    if wanted is not None:
        stmt = stmt.where(DownloadRequest.status.in_(wanted))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(math.ceil(total / REQUEST_PAGE), 1)
    page_no = min(max(page_no, 1), pages)
    found = db.scalars(
        stmt.order_by(DownloadRequest.id.desc())
        .offset((page_no - 1) * REQUEST_PAGE)
        .limit(REQUEST_PAGE)
    )
    rows = [requests_web._row(db, r) for r in found]
    query = {"status": status} if status != "all" else {}
    return {
        "rows": rows,
        "status": status,
        "filters": REQUEST_FILTERS,
        "total": total,
        "page_no": page_no,
        "pages": pages,
        "polling": any(r["r"].status in requests_web.RUNNING for r in rows),
        "all_users": auth.user.is_admin,
        "here": "/m/requests"
        + (f"?{urlencode({**query, 'page_no': page_no})}" if query or page_no > 1 else ""),
        "prev_url": "/m/requests?" + urlencode({**query, "page_no": page_no - 1}),
        "next_url": "/m/requests?" + urlencode({**query, "page_no": page_no + 1}),
    }


@router.get("/requests")
def requests_list(request: Request, db: DB, auth: UserAuth, status: str = "all", page_no: int = 1):
    return page(request, "requests.html", _request_list(db, auth, status, page_no), "requests")


def _request_detail(request: Request, db: DB, auth: UserAuth, request_id: int) -> dict:
    ctx = requests_web._detail_context(db, auth, requests_web._get(db, auth, request_id))
    ctx["job"] = None  # the jobs are for the desktop
    ctx["top_candidates"] = ctx["candidates"][:8]
    return ctx


@router.get("/requests/{request_id}")
def request_detail(request: Request, db: DB, auth: UserAuth, request_id: int):
    return page(request, "request.html", _request_detail(request, db, auth, request_id), "requests")


@router.get("/requests/{request_id}/body")
def request_body(request: Request, db: DB, auth: UserAuth, request_id: int):
    return page(
        request, "_request_body.html", _request_detail(request, db, auth, request_id), "requests"
    )


@router.post("/requests/{request_id}/choose")
def request_choose(
    request: Request, db: DB, auth: UserAuth, request_id: int, index: Annotated[int, Form()] = -1
):
    return to_mobile(requests_web.request_choose(request, db, auth, request_id, index))


@router.post("/requests/{request_id}/release")
def request_release(
    request: Request, db: DB, auth: UserAuth, request_id: int, choice: Annotated[str, Form()] = ""
):
    return to_mobile(requests_web.request_release(request, db, auth, request_id, choice))


@router.post("/requests/{request_id}/cancel")
def request_cancel(request: Request, db: DB, auth: UserAuth, request_id: int):
    return to_mobile(requests_web.request_cancel(request, db, auth, request_id, ""))


@router.post("/requests/{request_id}/retry")
def request_retry(request: Request, db: DB, auth: UserAuth, request_id: int):
    return to_mobile(requests_web.request_retry(request, db, auth, request_id, "", "", ""))


@router.post("/requests/{request_id}/delete")
def request_delete(request: Request, db: DB, auth: UserAuth, request_id: int):
    return to_mobile(requests_web.request_delete(request, db, auth, request_id, ""))


# ------------------------------------------------------------------ preferences


def _preferences(request: Request, auth: UserAuth, **extra) -> dict:
    return {
        "languages": LANG_NAMES,
        "current_language": current_lang(request),
        "themes": THEMES,
        "current_theme": auth.user.theme if auth.user.theme in THEMES else "auto",
        "homepage": "https://github.com/pernastefano/diskko",
        **extra,
    }


@router.get("/preferences")
def preferences(request: Request, auth: UserAuth):
    return page(request, "preferences.html", _preferences(request, auth), "")


@router.post("/preferences")
def preferences_save(
    request: Request,
    db: DB,
    settings: SettingsDep,
    auth: UserAuth,
    language: Annotated[str, Form()] = "",
    theme: Annotated[str, Form()] = "",
):
    """Language and theme. The quality of one's own requests is a desktop preference: it stays."""
    quality = get_setting(db, USER_QUALITY_KEY, "", auth.user.id)
    response = preferences_web.preferences_save(
        request, db, settings, auth, language, theme, quality
    )
    if response.status_code == 400:  # not a language or theme we have
        ctx = _preferences(request, auth, error="error.preferences_invalid")
        return page(request, "preferences.html", ctx, "", status_code=400)
    response.headers["location"] = "/m/preferences?flash=preferences_saved"
    return response


@router.post("/preferences/password")
def preferences_password(
    request: Request,
    db: DB,
    settings: SettingsDep,
    auth: UserAuth,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    new_password_confirm: Annotated[str, Form()] = "",
):
    try:
        auth_web.change_password(
            db, settings, auth, current_password, new_password, new_password_confirm
        )
    except UserError as exc:
        ctx = _preferences(request, auth, error=f"error.{exc.code}")
        return page(request, "preferences.html", ctx, "", status_code=400)
    return RedirectResponse("/m/preferences?flash=password_changed", status_code=303)
