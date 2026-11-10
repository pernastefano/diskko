"""Re-tag an album that is in the library from its MusicBrainz release.

Two modes with the same code: a preview (`apply` false) reads the files and the release and saves
what would change, and a run (`apply` true) does it. Both leave a report for the album page:
the tags now and after, the tracks of the release no file stands for, the files with no track.
Only albums matched to MusicBrainz with certainty are done, only the files the library tracks
for the album are touched, and a file is replaced only by a tagged copy that was checked.
"""

import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import select

from app.integrations.cached_http import ServiceError
from app.integrations.musicbrainz_client import MusicBrainzError
from app.jobs.context import JobContext
from app.jobs.errors import PermanentJobError
from app.jobs.handlers.postprocess import build_plan, find_cover, load_releases
from app.jobs.registry import job_handler
from app.models import (
    SOURCE_FILESYSTEM,
    SOURCE_MUSICBRAINZ,
    Artist,
    LibraryAlbum,
    LibraryTrack,
    ReleaseGroup,
    RetagReport,
    utcnow,
)
from app.services import plex_refresh as plex_svc
from app.services import retag
from app.services.importer import library_import as lib
from app.services.importer.integrity import IMAGE_EXTENSIONS, pick_cover_file
from app.services.importer.tagging import TaggingError
from app.services.quality_profiles import load_profiles

MAX_FOLDER_IMAGES = 20


def retag_dedupe_key(album_id: int) -> str:
    return f"retag_album:{album_id}"


def save_report(
    db, album_id: int, job_id: int | None, mode: str, status: str, options: dict, report: dict
) -> None:
    """The one report of an album: replaced by every preview and every run."""
    row = db.scalar(select(RetagReport).where(RetagReport.album_id == album_id))
    if row is None:
        row = RetagReport(album_id=album_id, mode=mode)
        db.add(row)
    row.job_id, row.mode, row.status = job_id, mode, status
    row.options_json, row.report_json = json.dumps(options), json.dumps(report)
    row.created_at = utcnow()
    db.commit()


def eligible_error(album: LibraryAlbum | None, group: ReleaseGroup | None) -> str | None:
    """Why this album cannot be re-tagged (a key of retag.error.*), or None."""
    if album is None or group is None:
        return "not_found"
    if album.match_status not in ("matched", "manual"):  # certain, or chosen by an admin
        return "not_matched"
    if group.source != SOURCE_MUSICBRAINZ:
        return "not_musicbrainz"
    return None


@job_handler("retag_album", queue="interactive", max_attempts=2, backoff_base=60)
async def retag_album(ctx: JobContext) -> None:
    album_id = int(ctx.payload["album_id"])
    apply = bool(ctx.payload.get("apply"))
    options = {
        "rename": bool(ctx.payload.get("rename")),
        "replace_cover": bool(ctx.payload.get("replace_cover")),
        "release_mbid": str(ctx.payload.get("release_mbid") or "") or None,
    }
    mode = "applied" if apply else "preview"
    settings = ctx.settings
    music = Path(settings.music_dir)

    def load(db):
        album = db.get(LibraryAlbum, album_id)
        group = (
            db.get(ReleaseGroup, album.release_group_id)
            if album and album.release_group_id
            else None
        )
        problem = eligible_error(album, group)
        if problem:
            return {"error": problem}
        artist = db.get(Artist, group.artist_id)
        stored = db.scalars(
            select(LibraryTrack.path).where(LibraryTrack.album_id == album.id)
        ).all()
        folder = lib.local_path(music, album.path)
        paths, absent = [], 0
        for stored_path in stored:
            found = lib.local_path(music, stored_path)
            if found is not None and found.is_file() and (folder and found.is_relative_to(folder)):
                paths.append(found)
            else:
                absent += 1
        shared = bool(folder and lib.other_albums_files(db, music, folder, [album.id]))
        return {
            "folder": folder,
            "paths": sorted(set(paths)),
            "absent": absent,
            "shared": shared,
            "source": album.source,
            "title": album.title,
            "artist": artist.name,
            "profiles": load_profiles(db),
            "group": {
                "id": group.id,
                "mbid": group.mbid,
                "title": group.title,
                "year": group.year,
                "primary_type": group.primary_type,
                "secondary_json": group.secondary_types_json,
                "source": group.source,
                "artist_id": group.artist_id,
                "hint": group.track_count_hint,
            },
        }

    async def fail(code: str) -> None:
        await ctx.run_db(
            lambda db: save_report(
                db, album_id, ctx.job_id, mode, "error", options, {"error": code}
            )
        )
        raise PermanentJobError(f"retag.error.{code}")

    info = await ctx.run_db(load)
    if "error" in info:
        await fail(info["error"])
    folder: Path | None = info["folder"]
    if folder is None or not folder.is_dir() or not info["paths"]:
        await fail("no_files")
    group, artist_name = info["group"], info["artist"]
    await ctx.log(f"{artist_name} – {info['title']}: {len(info['paths'])} file(s)")

    try:
        releases = await load_releases(ctx, group)
    except (MusicBrainzError, ServiceError):
        raise  # the engine tries again later
    if not releases:
        await fail("no_release")
    await ctx.progress(20, "Release")

    filesystem = info["source"] == SOURCE_FILESYSTEM
    plan = await asyncio.to_thread(
        retag.build,
        folder,
        info["paths"],
        releases,
        group,
        artist_name,
        build_plan,
        release_mbid=options["release_mbid"],
        rename=options["rename"] and filesystem,
        file_template=settings.import_file_template,
        missing_files=info["absent"],
    )
    if options["rename"] and not filesystem:
        plan.notes.append("rename_filesystem_only")
    if options["rename"] and filesystem and plan.folder_values is not None:
        if info["shared"]:
            plan.notes.append("folder_shared")
        else:
            target = lib.final_folder(settings, settings.import_folder_template, plan.folder_values)
            if target.resolve() != folder.resolve() and not target.exists():
                plan.folder_target = target
            elif target.resolve() != folder.resolve():
                plan.notes.append("folder_exists")
    if plan.chosen is not None:
        await ctx.log(
            f"MusicBrainz release: {plan.chosen.release.title} "
            f"({len(plan.chosen.pairs)} of {len(plan.chosen.release.tracks)} tracks have a file)"
        )
    for track in plan.missing:
        await ctx.log(f"No file for track {track.disc}-{track.position}: {track.title}", "warning")
    await ctx.progress(40, "Tags compared")
    await ctx.checkpoint()

    # ---- the cover: for the files without one (or all of them when asked)
    cover = None
    wanted = retag.choose_cover_targets(plan, options["replace_cover"])
    plan.cover = {
        "files_without": len([i for i in plan.items if i.taggable and not i.has_cover]),
        "replace": options["replace_cover"],
        "source": "",
    }
    if wanted:
        images = await asyncio.to_thread(_folder_images, folder)
        cover = await find_cover(ctx, group, artist_name, images)
        if cover is None:
            retag.choose_cover_targets(plan, False)
            for item in plan.items:
                item.set_cover = False
        else:
            plan.cover["source"], plan.cover["bytes"] = cover.source, len(cover.data)
    await ctx.progress(55, "Cover")

    if not apply:
        await ctx.run_db(
            lambda db: save_report(db, album_id, ctx.job_id, mode, "ready", options, plan.report())
        )
        await ctx.log(
            f"Preview ready: {len(plan.to_write)} file(s) to write, {len(plan.missing)} missing"
        )
        await ctx.progress(100, "Done")
        return

    # ---- the real thing
    work = plan.to_write
    for n, item in enumerate(work, start=1):
        await ctx.checkpoint()
        await ctx.progress(55 + 35 * n / max(len(work), 1), item.path.name)
        try:
            await asyncio.to_thread(retag.write_item, item, cover)
            item.state = "written"
        except TaggingError as exc:
            item.state, item.error = "failed", str(exc)
            await ctx.log(f"{item.path.name}: {exc}", "error")
    written = len([i for i in plan.items if i.state == "written"])
    failed = len([i for i in plan.items if i.state == "failed"])
    await ctx.log(f"{written} file(s) tagged, {failed} failed")

    final = folder
    if options["rename"] and filesystem and written:
        final = await asyncio.to_thread(_rename_all, plan, folder, music, ctx)
        for line in plan.notes:
            if line.startswith("rename_failed:"):
                await ctx.log(line, "warning")

    # ---- the library and Plex learn what changed
    if filesystem and written:
        await asyncio.to_thread(
            lib.register,
            ctx.session_factory,
            music,
            final,
            info["profiles"],
            drop_albums=[album_id],
            match_artist_ids=[group["artist_id"]],
        )
    elif written and not filesystem:
        await ctx.log("Plex library: the new tags are read at the next scan of Plex")
    await _tell_plex(ctx, folder, final, plan)

    status = "ready"
    await ctx.run_db(
        lambda db: save_report(db, album_id, ctx.job_id, "applied", status, options, plan.report())
    )
    await ctx.progress(100, "Done")
    if failed:
        raise PermanentJobError(f"{failed} file(s) could not be tagged")


def _folder_images(folder: Path) -> list[Path]:
    found = [
        p
        for p in sorted(folder.iterdir())
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXTENSIONS
    ]
    picked = pick_cover_file(found[:MAX_FOLDER_IMAGES])
    return [picked] if picked else []


def _rename_all(plan: retag.Plan, folder: Path, music: Path, ctx: JobContext) -> Path:
    """The new file names, then the new folder name. What cannot be renamed stays as it is."""
    for item in plan.items:
        if item.new_name and item.state in ("written", "same"):
            try:
                item.path = retag.rename_item(item)
            except OSError as exc:
                plan.notes.append(f"rename_failed:{item.path.name}: {exc}")
                item.new_name = None
    target = plan.folder_target
    if target is None:
        return folder
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(folder, target)
    except OSError as exc:
        plan.notes.append(f"rename_failed:{folder.name}: {exc}")
        plan.folder_target = None
        return folder
    lib.prune_empty(folder.parent, music)
    return target


async def _tell_plex(ctx: JobContext, old: Path, new: Path, plan: retag.Plan) -> None:
    """Plex scans the folder again; whatever goes wrong there never fails the job."""
    settings = ctx.settings
    if not plan.to_write and new == old:
        return
    try:
        if plex_svc.refresh_enabled(settings):
            items = [plex_svc.Pending(str(new), plan.chosen.release.title if plan.chosen else "")]
            if new != old and old.parent.is_dir():
                items.append(plex_svc.Pending(str(old.parent), "", check=False))
            await ctx.run_db(
                lambda db: plex_svc.request_refresh(db, settings, items, parent_job_id=ctx.job_id)
            )
            await ctx.log("Plex will be told about the album")
    except Exception as exc:  # noqa: BLE001 - the files are tagged, Plex is a courtesy
        await ctx.log(f"Plex could not be notified ({type(exc).__name__})", "warning")
