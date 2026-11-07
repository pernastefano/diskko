"""The album page: tracks, playing owned files from disk, previews and the download request.

Slow things (the MusicBrainz track list, the preview lookup) are jobs: the page queues them,
shows what it has and polls until they are done.
"""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.auth.deps import DB, AdminAuth, UserAuth
from app.i18n import has_key
from app.jobs import engine
from app.jobs.handlers.fetch_tracklist import tracklist_dedupe_key
from app.jobs.handlers.refresh_album import refresh_dedupe_key
from app.jobs.handlers.resolve_previews import previews_dedupe_key
from app.models import (
    REQ_ACTIVE,
    Artist,
    DownloadRequest,
    LibraryAlbum,
    LibraryTrack,
    PreviewLookup,
    ReleaseGroup,
)
from app.services import downloads as downloads_svc
from app.services.albums import (
    TrackRow,
    attach_artists,
    extension_of,
    media_type_of,
    owned_album_for,
    owned_rows,
    remote_rows,
    resolve_library_file,
    total_seconds,
)
from app.services.downloads import slskd_configured
from app.services.previews import preview_state
from app.services.tracklists import load_tracks, tracklist_state
from app.web.routers.discovery import job_state, queue_job
from app.web.routers.jobs import notify_worker
from app.web.templating import is_htmx, render

router = APIRouter()


def _summary(rows: list[TrackRow]) -> dict:
    return {
        "tracks": len(rows),
        "seconds": total_seconds(rows),
        "discs": len({r.disc for r in rows}),
    }


def _base_context(group: ReleaseGroup | None, album: LibraryAlbum | None, artist: Artist) -> dict:
    return {
        "artist": artist,
        "group": group,
        "album": album,
        "title": group.title if group else album.title,
        "year": group.year if group else album.year,
    }


def _remote_state(
    request: Request, db: DB, auth: UserAuth, group: ReleaseGroup, *, enqueue: bool, refresh: bool
) -> dict:
    """Tracks and previews of an album that is not in the library (queueing what is missing)."""
    settings = request.app.state.settings
    state = {"loading": False, "tracks_failed": False, "previews_failed": False}

    tracks = load_tracks(db, group.id)
    on_musicbrainz = group.source == "musicbrainz"  # other sources have no MusicBrainz tracklist
    if (
        on_musicbrainz
        and tracklist_state(db, group.id) != "fresh"
        and settings.mb_user_agent.strip()
    ):
        key = tracklist_dedupe_key(group.id)
        job = job_state(db, key)
        if job == "active":
            state["loading"] = not tracks
        elif job == "failed":
            state["tracks_failed"] = not tracks
        elif enqueue:
            queue_job(request, db, auth, "fetch_tracklist", {"release_group_id": group.id}, key)
            state["loading"] = not tracks

    preview = preview_state(db, group.id)
    previews = list(preview.tracks or []) if preview.state == "ready" else []
    if preview.state in ("unknown", "stale") or (refresh and preview.state == "ready"):
        key = previews_dedupe_key(group.id)
        job = job_state(db, key)
        if job == "active":
            state["loading"] = True
        elif job == "failed" and not refresh:
            state["previews_failed"] = True
        elif enqueue:
            force = preview.state != "unknown"
            queue_job(
                request,
                db,
                auth,
                "resolve_previews",
                {"release_group_id": group.id, "force": force},
                key,
            )
            state["loading"] = True
        previews = list(preview.tracks or []) if preview.tracks else previews
    state["rows"] = remote_rows(tracks, previews)
    attach_artists(db, state["rows"], db.get(Artist, group.artist_id))
    state["lookup"] = db.get(PreviewLookup, group.id) if previews else None
    state["tracks_known"] = bool(tracks)
    return state


def _group_context(
    request: Request, db: DB, auth: UserAuth, group: ReleaseGroup, *, enqueue: bool, refresh=False
) -> dict:
    artist = db.get(Artist, group.artist_id)
    owned = owned_album_for(db, group.id)
    ctx = _base_context(group, owned, artist)
    if owned is not None:
        rows = owned_rows(db, owned)
        attach_artists(db, rows, artist)
        ctx.update(mode="owned", rows=rows, loading=False, lookup=None, tracks_known=True)
        ctx["upgrade"] = _upgrade_context(request, db, group, owned)
    else:
        remote = _remote_state(request, db, auth, group, enqueue=enqueue, refresh=refresh)
        ctx.update(mode="remote", **remote)
        info = downloads_svc.request_infos(db, [group.id]).get(group.id)
        ctx.update(
            status=info.status if info else None,
            info=info,
            g=group,
            unavailable=not slskd_configured(request.app.state.settings),
        )
    ctx["summary"] = _summary(ctx["rows"])
    return ctx


def _upgrade_context(request: Request, db: DB, group: ReleaseGroup, album: LibraryAlbum) -> dict:
    """What the "upgrade the quality" section of an owned album needs."""
    profiles = request.app.state.quality_profiles
    minimum = request.app.state.settings.default_quality
    info = downloads_svc.request_infos(db, [group.id]).get(group.id)
    below = downloads_svc.below_minimum(profiles, album.quality_tier, minimum)
    running = bool(info and info.status in REQ_ACTIVE)
    return {
        "group": group,
        "album": album,
        "current": profiles.name(album.quality_tier),
        "minimum": profiles.name(minimum),
        "minimum_key": minimum,
        # offered only to an album that fails the minimum quality (or while a request runs)
        "offer": below or running,
        "targets": downloads_svc.upgrade_targets(profiles, album.quality_tier, minimum),
        "info": info,
        "req": db.get(DownloadRequest, info.id) if info else None,
        "unavailable": not slskd_configured(request.app.state.settings),
    }


def _owned_group_or_404(db: DB, group_id: int) -> tuple[ReleaseGroup, LibraryAlbum]:
    group = db.get(ReleaseGroup, group_id)
    album = owned_album_for(db, group_id) if group else None
    if group is None or album is None:
        raise HTTPException(status_code=404, detail="not_found")
    return group, album


@router.get("/album/{group_id}/upgrade")
def upgrade_poll(request: Request, db: DB, auth: UserAuth, group_id: int):
    group, album = _owned_group_or_404(db, group_id)
    return render(request, "_album_upgrade.html", _upgrade_context(request, db, group, album))


@router.post("/album/{group_id}/upgrade")
def upgrade_request(
    request: Request,
    db: DB,
    auth: UserAuth,
    group_id: int,
    target: Annotated[str, Form()] = "",
):
    """Ask for a better copy of an owned album: a download request that aims at `target`."""
    group, album = _owned_group_or_404(db, group_id)
    settings = request.app.state.settings
    profiles = request.app.state.quality_profiles
    wanted = profiles.get(target)
    error = None
    if not slskd_configured(settings):
        error = "request_no_slskd"
    elif not downloads_svc.below_minimum(profiles, album.quality_tier, settings.default_quality):
        error = "request_meets_minimum"
    elif wanted is None or wanted not in downloads_svc.upgrade_targets(
        profiles, album.quality_tier, settings.default_quality
    ):
        error = "request_bad_target"
    if error:
        if is_htmx(request):
            ctx = _upgrade_context(request, db, group, album) | {"error": f"error.{error}"}
            return render(request, "_album_upgrade.html", ctx, status_code=400)
        return RedirectResponse(f"/album/{group_id}?error={error}", status_code=303)
    new, created = downloads_svc.create_request(
        db,
        auth.user.id,
        group.id,
        default_mode=settings.slskd_mode,
        quality_key=wanted.key,
        upgrade_from=album.quality_tier,
    )
    if created:
        engine.enqueue(
            db,
            "slskd_search",
            {"request_id": new.id},
            dedupe_key=downloads_svc.search_dedupe_key(new.id),
            created_by=auth.user.id,
        )
        db.commit()
        notify_worker(request)
    if is_htmx(request):
        return render(request, "_album_upgrade.html", _upgrade_context(request, db, group, album))
    return RedirectResponse(f"/album/{group_id}", status_code=303)


@router.get("/album/local/{album_id}")
def local_album_page(request: Request, db: DB, auth: UserAuth, album_id: int):
    """An album of the library that MusicBrainz could not be matched to."""
    album = db.get(LibraryAlbum, album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="not_found")
    if album.release_group_id and album.match_status in ("matched", "tentative", "manual"):
        return RedirectResponse(f"/album/{album.release_group_id}", status_code=303)
    artist = db.get(Artist, album.artist_id)
    ctx = _base_context(None, album, artist)
    rows = owned_rows(db, album)
    attach_artists(db, rows, artist)
    ctx.update(mode="local", rows=rows, loading=False, lookup=None, tracks_known=True)
    ctx["summary"] = _summary(rows)
    return render(request, "album.html", ctx)


@router.post("/album/{group_id}/refresh")
def album_refresh(request: Request, db: DB, auth: AdminAuth, group_id: int):
    """Update the album's metadata: a job re-reads it at MusicBrainz and queues the rest."""
    group = db.get(ReleaseGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="not_found")
    engine.enqueue(
        db,
        "refresh_album",
        {"release_group_id": group.id},
        dedupe_key=refresh_dedupe_key(group.id),
        created_by=auth.user.id,
    )
    notify_worker(request)
    return RedirectResponse(f"/album/{group.id}?flash=album_refresh_queued", status_code=303)


@router.get("/album/{group_id}")
def album_page(request: Request, db: DB, auth: UserAuth, group_id: int):
    group = db.get(ReleaseGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="not_found")
    # The page itself only reads: its tracks section asks for the missing pieces (a POST).
    ctx = _group_context(request, db, auth, group, enqueue=False)
    failed = f"error.{request.query_params.get('error', '')}"  # after a refused plain form post
    ctx["page_error"] = failed if has_key(failed) else None
    return render(request, "album.html", ctx)


@router.post("/album/{group_id}/tracks")
def album_tracks_load(request: Request, db: DB, auth: UserAuth, group_id: int, refresh: str = ""):
    """Called when the page opens (and by "reload"): queues what is missing, returns the tracks."""
    group = db.get(ReleaseGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="not_found")
    ctx = _group_context(request, db, auth, group, enqueue=True, refresh=refresh == "1")
    return render(request, "_album_tracks.html", ctx)


@router.get("/album/{group_id}/tracks")
def album_tracks_poll(request: Request, db: DB, auth: UserAuth, group_id: int):
    group = db.get(ReleaseGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="not_found")
    ctx = _group_context(request, db, auth, group, enqueue=False)
    return render(request, "_album_tracks.html", ctx)


@router.get("/stream/{track_id}")
def stream_track(request: Request, db: DB, auth: UserAuth, track_id: int):
    """The audio file of a library track, with Range support (seeking). By track id only."""
    track = db.get(LibraryTrack, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="not_found")
    if track.path.startswith("plex:"):  # Plex knows it by address only: there is no file here
        raise HTTPException(status_code=404, detail="not_found")
    media_type = media_type_of(extension_of(track.path))
    if media_type is None:
        raise HTTPException(status_code=415, detail="not_playable")
    path = resolve_library_file(request.app.state.settings.music_dir, track.path)
    if path is None:
        raise HTTPException(status_code=404, detail="not_found")
    return FileResponse(
        path, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"}
    )
