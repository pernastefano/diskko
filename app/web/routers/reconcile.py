"""Matching the files of a download with MusicBrainz by hand: which release it is, which file is
which track, and, when the album found is the wrong one, another album of MusicBrainz.

The page of a request that waits for its release shows the files next to the tracks of a release
(the pairing the engine would make, which can be changed), the other releases, and a search over
MusicBrainz. The searches and the import are jobs; the pages only read what they left.
"""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, UserAuth
from app.integrations.musicbrainz_client import (
    peek_release_group_search,
    release_group_query,
)
from app.jobs import engine
from app.jobs.handlers.reconcile_options import reconcile_dedupe_key
from app.jobs.handlers.search_release_groups import search_dedupe_key
from app.models import REQ_AWAITING_CHOICE, Artist, ReleaseGroup
from app.services import album_match as match_svc
from app.services import audit
from app.services import downloads as svc
from app.web.routers.discovery import job_state
from app.web.routers.jobs import notify_worker
from app.web.routers.requests import _get, _label, _owner_name
from app.web.templating import render

router = APIRouter()

MODES = ("title", "anything")
SCOPES = ("artist", "all")


def _page(request_id: int, **query: str) -> str:
    extra = urlencode({k: v for k, v in query.items() if v})
    return f"/requests/{request_id}/reconcile" + (f"?{extra}" if extra else "")


def _error(request_id: int, code: str) -> RedirectResponse:
    return RedirectResponse(f"/requests/{request_id}/reconcile?error={code}", status_code=303)


def _search_query(group: ReleaseGroup | None, artist: Artist | None, q: str, mode: str, scope: str):
    """(query text for MusicBrainz, normalized mode and scope)."""
    mode = mode if mode in MODES else "title"
    scope = scope if scope in SCOPES else "all"
    artist_mbid = artist.mbid if (artist and artist.mbid and scope == "artist") else None
    text = q.strip()[:200]
    return (release_group_query(text, mode, artist_mbid) if text else ""), text, mode, scope


def _context(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    release: str,
    q: str,
    mode: str,
    scope: str,
) -> dict:
    req = _get(db, auth, request_id)
    state = svc.release_state_of(req)
    group = db.get(ReleaseGroup, req.release_group_id)
    artist = db.get(Artist, group.artist_id) if group else None
    options = state["options"]
    chosen = next((o for o in options if o["mbid"] == release), options[0] if options else None)
    query, text, mode, scope = _search_query(
        group, artist, q or (group.title if group else ""), mode, scope
    )
    search = {"state": "idle", "hits": []}
    if q and query and request.app.state.settings.mb_user_agent.strip():
        hits = peek_release_group_search(db, query, match_svc.SEARCH_LIMIT)
        if hits is not None:
            search = {"state": "results", "hits": hits}
        else:
            job = job_state(db, search_dedupe_key(query))
            search["state"] = {"active": "searching", "failed": "failed"}.get(job, "idle")
    elif q and not request.app.state.settings.mb_user_agent.strip():
        search["state"] = "not_configured"
    return {
        "r": req,
        "group": group,
        "artist": artist,
        "label": _label(db, req),
        "state": state,
        "options": options,
        "chosen": chosen,
        "waiting": req.status == REQ_AWAITING_CHOICE,
        "q": q or (group.title if group else ""),
        "searched": bool(q),
        "mode": mode,
        "scope": scope,
        "modes": MODES,
        "scopes": SCOPES,
        "can_scope": bool(artist and artist.mbid),
        "search": search,
        "form_query": urlencode({"q": text, "mode": mode, "scope": scope}),
    }


@router.get("/requests/{request_id}/reconcile")
def reconcile_page(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    release: str = "",
    q: str = "",
    mode: str = "title",
    scope: str = "all",
):
    ctx = _context(request, db, auth, request_id, release, q, mode, scope)
    if not ctx["waiting"]:
        return RedirectResponse(f"/requests/{request_id}", status_code=303)
    return render(request, "reconcile.html", ctx)


@router.get("/requests/{request_id}/reconcile/results")
def reconcile_results(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    q: str = "",
    mode: str = "title",
    scope: str = "all",
):
    """The search results alone; polled while the search runs."""
    ctx = _context(request, db, auth, request_id, "", q, mode, scope)
    return render(request, "_reconcile_search.html", ctx)


@router.post("/requests/{request_id}/reconcile/rebuild")
def reconcile_rebuild(request: Request, db: DB, auth: UserAuth, request_id: int):
    """Match the files with MusicBrainz again from scratch (rebuilds what the page shows)."""
    req = _get(db, auth, request_id)
    try:
        svc.begin_reconcile(db, req)
    except svc.RequestError as exc:
        return _error(request_id, exc.code)
    engine.enqueue(
        db,
        "postprocess",
        {"request_id": req.id, "release_choice": svc.RELEASE_RECONCILE},
        dedupe_key=svc.postprocess_dedupe_key(req.id),
        created_by=auth.user.id,
    )
    db.commit()
    notify_worker(request)
    return RedirectResponse(f"/requests/{request_id}", status_code=303)


@router.post("/requests/{request_id}/reconcile/find")
def reconcile_find(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    q: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "title",
    scope: Annotated[str, Form()] = "all",
):
    """Search MusicBrainz for another album (a job); the page shows the answer when it is in."""
    ctx = _context(request, db, auth, request_id, "", q, mode, scope)
    query = release_group_query(ctx["q"], ctx["mode"], None)
    if ctx["scope"] == "artist" and ctx["can_scope"]:
        query = release_group_query(ctx["q"], ctx["mode"], ctx["artist"].mbid)
    target = _page(request_id, q=ctx["q"], mode=ctx["mode"], scope=ctx["scope"])
    if ctx["q"].strip() and request.app.state.settings.mb_user_agent.strip():
        key = search_dedupe_key(query)
        if job_state(db, key) != "active":
            engine.enqueue(
                db,
                "search_release_groups",
                {"query": query},
                dedupe_key=key,
                created_by=auth.user.id,
                label=f"{ctx['label']}: {ctx['q']}"[:255],
            )
            db.commit()
            notify_worker(request)
    return RedirectResponse(target + "#find", status_code=303)


@router.post("/requests/{request_id}/reconcile/group")
def reconcile_group(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    choice: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "title",
    scope: Annotated[str, Form()] = "all",
):
    """The files are another album: take the release group the user picked from the search."""
    ctx = _context(request, db, auth, request_id, "", q, mode, scope)
    req, artist = ctx["r"], ctx["artist"]
    query = release_group_query(
        ctx["q"], ctx["mode"], artist.mbid if ctx["scope"] == "artist" and artist else None
    )
    hits = peek_release_group_search(db, query, match_svc.SEARCH_LIMIT) or []
    hit = next((h for h in hits if choice == f"mbid:{h.data.mbid}"), None)
    if hit is None:  # the cached answer is gone: the choice cannot be trusted
        return _error(request_id, "search_expired")
    group = match_svc.adopt_hit(db, artist.id, hit)
    try:
        svc.switch_group(db, req, group)
    except svc.RequestError as exc:
        db.rollback()
        return _error(request_id, exc.code)
    engine.enqueue(
        db,
        "reconcile_options",
        {"request_id": req.id},
        dedupe_key=reconcile_dedupe_key(req.id),
        created_by=auth.user.id,
    )
    audit.record(
        db,
        auth.user,
        "request.album_changed",
        target_type="request",
        target_id=req.id,
        label=_label(db, req),
        details={"group": group.mbid, "owner": _owner_name(db, req)},
    )
    db.commit()
    notify_worker(request)
    return RedirectResponse(_page(request_id), status_code=303)


@router.post("/requests/{request_id}/reconcile/import")
def reconcile_import(
    request: Request,
    db: DB,
    auth: UserAuth,
    request_id: int,
    release: Annotated[str, Form()] = "",
    map: Annotated[list[str] | None, Form()] = None,  # noqa: A002 - the field of the form
):
    """Import with the pairing of files and tracks the user set for a release."""
    req = _get(db, auth, request_id)
    files = svc.release_state_of(req)["files"]
    mapping: dict[str, str] = {}
    for item in map or []:
        index, _, key = item.partition("|")
        if not index.isdigit() or int(index) >= len(files):
            return _error(request_id, "request_bad_mapping")
        mapping[files[int(index)]["name"]] = key
    try:
        svc.choose_release_option(db, req, release, mapping)
    except svc.RequestError as exc:
        return _error(request_id, exc.code)
    engine.enqueue(
        db,
        "postprocess",
        {"request_id": req.id, "release_choice": release, "mapping": mapping},
        dedupe_key=svc.postprocess_dedupe_key(req.id),
        created_by=auth.user.id,
    )
    audit.record(
        db,
        auth.user,
        "request.release_chosen",
        target_type="request",
        target_id=req.id,
        label=_label(db, req),
        details={"choice": release, "by_hand": True, "owner": _owner_name(db, req)},
    )
    db.commit()
    notify_worker(request)
    return RedirectResponse(f"/requests/{request_id}", status_code=303)
