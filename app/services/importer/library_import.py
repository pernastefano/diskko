"""Putting an album in the library: staging, the atomic move, conflicts, quarantine, the database.

Nothing is changed in the download until the album is safely in the library: the files are
copied (and tagged) in a hidden folder inside the library, which is on the same file system as
the final place, so the last step is a single rename. When the folder already exists the staged
files overwrite the ones with the same name (one atomic replace each; a file that belongs to
another album is never touched). After an upgrade the old copy is removed, or moved aside when
KEEP_REPLACED is set.
"""

import contextlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import SOURCE_FILESYSTEM, LibraryAlbum, LibraryTrack
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
from app.services.library.tags import AUDIO_EXTENSIONS, UnreadableAudio, read_audio_file
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


@dataclass
class OldAlbum:
    """A copy of the album that is in the library now and is being replaced by the import."""

    id: int
    folder: Path  # where it lives (inside the library)
    files: list[Path] = field(default_factory=list)  # its tracks that are there on disk


def local_path(music_dir: Path, stored: str) -> Path | None:
    """The file or folder a stored library path stands for, only if it is inside the library.
    The library keeps paths relative to its root (Plex ones may be absolute: they are accepted
    when they point inside the root too); links that lead out of it are refused."""
    if not stored or stored.startswith("plex:"):
        return None
    root = Path(music_dir).resolve()
    candidate = Path(stored)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        real = candidate.resolve()
    except OSError:
        return None
    return real if real.is_relative_to(root) and real != root else None


def owned_albums(db: Session, music_dir: Path, release_group_id: int) -> list[OldAlbum]:
    """The library albums that are this release group, whichever source listed them (a library
    read from Plex has its own rows): the copies an upgrade replaces."""
    rows = db.scalars(
        select(LibraryAlbum).where(
            LibraryAlbum.release_group_id == release_group_id,
            LibraryAlbum.match_status.in_(("matched", "tentative", "manual")),
        )
    )
    found = []
    for album in rows:
        folder = local_path(music_dir, album.path)
        if folder is None:
            continue
        tracks = db.scalars(select(LibraryTrack.path).where(LibraryTrack.album_id == album.id))
        files = [p for stored in tracks if (p := local_path(music_dir, stored)) and p.is_file()]
        found.append(OldAlbum(album.id, folder, files))
    return found


def other_albums_files(
    db: Session, music_dir: Path, folder: Path, own_album_ids: list[int]
) -> set[Path]:
    """The tracked files inside `folder` that belong to other albums of the library: an import
    into a folder shared with them may overwrite its own files, never these."""
    music = Path(music_dir).resolve()
    prefixes = {f"{folder.resolve().relative_to(music).as_posix()}/", f"{folder.resolve()}/"}
    found: set[Path] = set()
    for prefix in prefixes:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = db.scalars(
            select(LibraryTrack.path).where(
                LibraryTrack.path.like(escaped + "%", escape="\\"),
                LibraryTrack.album_id.not_in(own_album_ids or [0]),
            )
        )
        found.update(p for stored in rows if (p := local_path(music_dir, stored)) is not None)
    return found


def merge_into(staging: Path, final: Path, protected: set[Path]) -> int:
    """Put the staged files in a folder that already exists, overwriting the files that are
    there with the same name (the marker goes last: its presence means the copy is complete).
    A file that belongs to another album of the folder is never overwritten. Returns how many
    files were overwritten."""
    final.mkdir(parents=True, exist_ok=True)
    staged = sorted(
        (p for p in staging.iterdir() if p.is_file()),
        key=lambda p: (p.name.startswith(MARKER_PREFIX), p.name),
    )
    for source in staged:
        target = final / source.name
        if target.resolve() in protected:
            raise ImportRejected(f"{target.name} in {final.name} belongs to another album")
        if target.is_dir():
            raise ImportRejected(f"{target.name} in {final.name} is a folder")
    overwritten = 0
    for source in staged:
        target = final / source.name
        overwritten += target.exists()
        os.replace(source, target)
    shutil.rmtree(staging, ignore_errors=True)
    return overwritten


_IMAGE_SIDECARS = {"cover", "folder", "front", "back", "albumart", "albumartsmall"}
_SIDECAR_SUFFIXES = {".cue", ".log", ".m3u", ".m3u8", ".nfo", ".accurip"}
_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}


def _is_sidecar(path: Path) -> bool:
    name = path.name.lower()
    if name in _JUNK or path.suffix.lower() in _SIDECAR_SUFFIXES:
        return True
    return path.suffix.lower() in (".jpg", ".jpeg", ".png") and path.stem.lower() in _IMAGE_SIDECARS


def _has_audio(folder: Path) -> bool:
    return any(p.suffix.lower() in AUDIO_EXTENSIONS for p in folder.rglob("*") if p.is_file())


def _prune_empty(folder: Path, music_dir: Path) -> None:
    """Remove `folder` and its parents while they are empty (never the library root)."""
    root = Path(music_dir).resolve()
    for sub in sorted((d for d in folder.rglob("*") if d.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            sub.rmdir()
    current = folder
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def retire_album(
    old: OldAlbum, music_dir: Path, keep: set[Path], final: Path, *, aside=None
) -> list[str]:
    """Take an old copy out of the library after its replacement is in place: its tracks go
    (except the files that are the new copy), and then, when its folder holds no audio any more
    and is not where the new copy lives, the cover and the other sidecars and the empty folders.
    `aside(path)` moves a file somewhere instead of deleting it. Returns what was left alone."""
    for path in old.files:
        if path.resolve() in keep or not path.is_file():
            continue
        if aside is not None:
            aside(path)
        else:
            path.unlink()
    folder = old.folder
    if not folder.is_dir() or folder == final or final.is_relative_to(folder):
        return []
    if _has_audio(folder):  # another album lives here: the folder stays as it is
        return []
    for path in [p for p in folder.rglob("*") if p.is_file() and _is_sidecar(p)]:
        path.unlink()
    left = [p.name for p in folder.rglob("*") if p.is_file()]
    _prune_empty(folder, music_dir)
    return left


def drop_albums(factory: sessionmaker[Session], album_ids: list[int]) -> None:
    """Forget library albums (and their tracks): the copies an upgrade replaced."""
    with factory() as db:
        for album_id in album_ids:
            if (row := db.get(LibraryAlbum, album_id)) is not None:
                db.delete(row)
        db.commit()


def set_aside_files(
    files: list[Path], folder: Path, replaced_root: Path, request_id: int, stamp: str
) -> Path:
    """Move only some files of a shared folder out of the library (never deleted)."""
    base = replaced_root / f"{request_id}-{stamp}" / folder.name
    base.mkdir(parents=True, exist_ok=True)
    for path in files:
        if path.is_file() and path.resolve().is_relative_to(folder.resolve()):
            target = base / path.name
            n = 2
            while target.exists():
                target = base / f"{path.stem} ({n}){path.suffix}"
                n += 1
            shutil.move(str(path), str(target))
    return base


MARKER_PREFIX = ".diskko-import-"


def mark_staged(staging: Path, request_id: int) -> None:
    """Put a hidden marker in the staged folder: it travels with the atomic rename, so a folder
    that has it was put in place by this very request (an earlier, interrupted run)."""
    (staging / f"{MARKER_PREFIX}{request_id}").write_text("diskko import in progress\n")


def is_marked(folder: Path, request_id: int) -> bool:
    return (folder / f"{MARKER_PREFIX}{request_id}").is_file()


def unmark(folder: Path, request_id: int) -> None:
    (folder / f"{MARKER_PREFIX}{request_id}").unlink(missing_ok=True)


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
