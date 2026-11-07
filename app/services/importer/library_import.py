"""Putting an album in the library: staging, the atomic move, conflicts, quarantine, the database.

Nothing is changed in the download until the album is safely in the library: the files are
copied (and tagged) in a hidden folder inside the library, which is on the same file system as
the final place, so the last step is a single rename. A folder that is already there is never
overwritten: an upgrade sets the old copy aside, anything else goes to the quarantine.
"""

import contextlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import SOURCE_FILESYSTEM, LibraryAlbum
from app.quality import ProfileSet
from app.services.importer.integrity import DownloadedFile, ImportRejected
from app.services.importer.naming import (
    NameValues,
    file_stem,
    folder_parts,
)
from app.services.importer.tagging import Cover, TrackTags, write_tags
from app.services.library.filesystem import to_scanned_track
from app.services.library.grouping import group_tracks
from app.services.library.scan import save_artists
from app.services.library.tags import UnreadableAudio, read_audio_file
from app.services.musicbrainz.matching import match_artist_albums

STAGING_DIR = ".diskko-import"  # hidden folders are not scanned
REPLACED_DIR = ".diskko-replaced"
DOWNLOAD_ROOT = "diskko"  # <downloads>/diskko/<request id> (see services.downloads.DEST_ROOT)


@dataclass
class PlannedFile:
    source: DownloadedFile
    tags: TrackTags
    name: NameValues  # for the file name
    taggable: bool


# ------------------------------------------------------------------------ places


def request_dir(settings: Settings, dest_dir: str | None, request_id: int) -> Path:
    """The download folder of a request, only if it lies under <downloads>/diskko."""
    root = Path(settings.downloads_dir).resolve()
    if not str(settings.downloads_dir).strip() or not root.is_dir():
        raise ImportRejected("the downloads folder is not available")
    folder = root / (dest_dir or f"{DOWNLOAD_ROOT}/{request_id}")
    base = root / DOWNLOAD_ROOT
    try:
        resolved = folder.resolve(strict=True)
    except OSError as exc:
        raise ImportRejected("the downloaded files are no longer in the downloads folder") from exc
    if not resolved.is_relative_to(base.resolve()) or resolved == base.resolve():
        raise ImportRejected("the download folder is outside the downloads area")
    return resolved


def quarantine_root(settings: Settings) -> Path:
    custom = settings.quarantine_dir.strip()
    return Path(custom) if custom else Path(settings.downloads_dir) / DOWNLOAD_ROOT / "quarantine"


def final_folder(settings: Settings, template: str, values: NameValues) -> Path:
    """The album folder in the library, guaranteed to stay under the music folder."""
    music = Path(settings.music_dir).resolve()
    target = music.joinpath(*folder_parts(template, values))
    if not target.resolve().is_relative_to(music) or target.resolve() == music:
        raise ImportRejected("the album folder would be outside the music folder")
    return target


# --------------------------------------------------------------------- quarantine


def quarantine(source: Path, root: Path, request_id: int, reason: str) -> Path:
    """Move a download that cannot be imported aside, with the reason next to it."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / str(request_id)
    n = 2
    while target.exists():
        target = root / f"{request_id}-{n}"
        n += 1
    shutil.move(str(source), str(target))
    stamp = datetime.now().isoformat(timespec="seconds")
    (target / "REASON.txt").write_text(f"{stamp}\n{reason}\n", encoding="utf-8")
    return target


# ------------------------------------------------------------------------ staging


def stage(
    plan: list[PlannedFile],
    staging: Path,
    *,
    file_template: str,
    multi_disc: bool,
    cover: Cover | None,
    cover_name: str = "cover",
    progress=None,
) -> list[Path]:
    """Copy the files into `staging` under their new names and tag them. Returns the new paths.
    The originals are not touched."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    done: list[Path] = []
    taken: set[str] = set()
    for i, item in enumerate(plan, start=1):
        suffix = item.source.path.suffix.lower()
        stem = file_stem(file_template, item.name, multi_disc=multi_disc)
        name = f"{stem}{suffix}"
        n = 2
        while name.lower() in taken:  # two tracks that would get the same name
            name = f"{stem} ({n}){suffix}"
            n += 1
        taken.add(name.lower())
        target = staging / name
        shutil.copy2(item.source.path, target)
        if item.taggable:
            write_tags(target, item.tags, cover)
        done.append(target)
        if progress:
            progress(i, len(plan), name)
    if cover is not None:
        extension = ".png" if cover.mime == "image/png" else ".jpg"
        (staging / f"{cover_name}{extension}").write_bytes(cover.data)
    return done


def clean_staging(music_dir: Path, request_id: int) -> None:
    """Remove the working folder of a request, and the hidden parent when it is left empty."""
    base = Path(music_dir) / STAGING_DIR
    shutil.rmtree(base / str(request_id), ignore_errors=True)
    with contextlib.suppress(OSError):  # another import is working there, or it is gone
        base.rmdir()


def verify_staged(paths: list[Path]) -> None:
    """The copies must open and have kept their length (a full disk or a bad copy shows here)."""
    for path in paths:
        try:
            info = read_audio_file(path)
        except UnreadableAudio as exc:
            raise ImportRejected(
                f"{path.name} is damaged after copying and tagging: {exc}"
            ) from exc
        if not info.duration:
            raise ImportRejected(f"{path.name} lost its audio while being copied")


# --------------------------------------------------------------------------- move


def owned_folders(db: Session, music_dir: Path, release_group_id: int) -> list[tuple[int, Path]]:
    """(album id, folder) of the library albums that are this release group."""
    rows = db.scalars(
        select(LibraryAlbum).where(
            LibraryAlbum.release_group_id == release_group_id,
            LibraryAlbum.source == SOURCE_FILESYSTEM,
            LibraryAlbum.match_status.in_(("matched", "tentative", "manual")),
        )
    )
    return [(a.id, Path(music_dir) / a.path) for a in rows if a.path]


def set_aside(folder: Path, replaced_root: Path, request_id: int, stamp: str) -> Path:
    """Move an old copy of an album out of the library (never deleted)."""
    base = replaced_root / f"{request_id}-{stamp}"
    base.mkdir(parents=True, exist_ok=True)
    target = base / folder.name
    n = 2
    while target.exists():
        target = base / f"{folder.name} ({n})"
        n += 1
    shutil.move(str(folder), str(target))
    return target


def commit(staging: Path, final: Path) -> None:
    """The one step that makes the album appear: a rename on the same file system."""
    if final.exists():
        raise ImportRejected(f"{final} already exists")
    final.parent.mkdir(parents=True, exist_ok=True)
    os.rename(staging, final)


# ---------------------------------------------------------------------- database


def register(
    factory: sessionmaker[Session],
    music_dir: Path,
    folder: Path,
    profiles: ProfileSet,
    *,
    drop_albums: list[int] = (),
    match_artist_ids: list[int] = (),
) -> list[int]:
    """Record the album now in `folder`: its tracks are read from the files and its artist and
    album saved, the albums it replaces are forgotten and the artists are matched again with
    MusicBrainz (the release group tag in the files makes this album owned). Returns album ids."""
    music = Path(music_dir).resolve()
    scanned = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            info = read_audio_file(path)
        except UnreadableAudio:
            continue
        stat = path.stat()
        relative = path.resolve().relative_to(music).as_posix()
        scanned.append(to_scanned_track(relative, stat.st_size, stat.st_mtime_ns, info, profiles))
    with factory() as db:
        for album_id in drop_albums:
            old = db.get(LibraryAlbum, album_id)
            if old is not None:
                db.delete(old)
        db.flush()
        artist_ids = save_artists(db, SOURCE_FILESYSTEM, group_tracks(scanned, profiles))
        for artist_id in {*artist_ids, *match_artist_ids}:
            match_artist_albums(db, artist_id)
        relative_dir = folder.resolve().relative_to(music).as_posix()
        ids = list(
            db.scalars(
                select(LibraryAlbum.id).where(
                    LibraryAlbum.source == SOURCE_FILESYSTEM, LibraryAlbum.path == relative_dir
                )
            )
        )
        db.commit()
    return ids
