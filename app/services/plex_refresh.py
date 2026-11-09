"""Telling Plex about albums that were just imported.

An import adds a folder to the library; Plex only knows about it after a scan. The folders to
show Plex wait in a list (the `plex_refresh.pending` setting) and a single job, started a little
later, scans them: imports that come close together share one scan. The job scans the album
folders (a partial scan: `path` is the folder as Plex sees it), and the whole section when a
partial scan is refused, did not make an album show up, or there are too many folders.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

import requests
from plexapi.exceptions import BadRequest, NotFound
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.jobs import engine
from app.models import SOURCE_FILESYSTEM, LibraryAlbum, LibraryTrack, utcnow
from app.services.app_settings import get_setting, set_setting
from app.services.library.plex_provider import map_path, open_section, parse_path_map

PENDING_KEY = "plex_refresh.pending"
LAST_KEY = "plex_refresh.last_finished"
JOB_KEY = "plex_refresh"
FULL_SCAN_PATHS = 20  # more folders than this: one scan of the whole section is cheaper


def plex_configured(settings: Settings) -> bool:
    return bool(settings.plex_url.strip() and settings.plex_token.strip())


def refresh_enabled(settings: Settings) -> bool:
    return settings.plex_refresh_enabled and plex_configured(settings)


# ------------------------------------------------------------------------- paths


def unmap_path(local: str, rules: list[tuple[str, str]]) -> str:
    """The folder as Plex sees it: the inverse of LIBRARY_PATH_MAP (`plex_prefix=local_prefix`).
    Without a rule that covers it, Plex is assumed to see the same path."""
    normalized = local.replace("\\", "/").rstrip("/")
    for plex_prefix, local_prefix in sorted(rules, key=lambda r: len(r[1]), reverse=True):
        local_prefix = local_prefix.replace("\\", "/").rstrip("/")
        if normalized == local_prefix or normalized.startswith(local_prefix + "/"):
            return plex_prefix + normalized[len(local_prefix) :]
    return normalized


# ------------------------------------------------------------ the list and the job


@dataclass(frozen=True)
class Pending:
    path: str  # the album folder, absolute, as this app sees it
    title: str  # the album title, to look for it in Plex afterwards
    check: bool = True  # False: only have Plex scan it (a folder an old copy was removed from)


def read_pending(db: Session) -> list[Pending]:
    items = get_setting(db, PENDING_KEY, []) or []
    return [
        Pending(i["path"], i.get("title", ""), i.get("check", True))
        for i in items
        if isinstance(i, dict) and "path" in i
    ]


def _write_pending(db: Session, items: list[Pending]) -> None:
    set_setting(
        db,
        PENDING_KEY,
        [
            {"path": i.path, "title": i.title, **({} if i.check else {"check": False})}
            for i in items
        ],
    )


def add_pending(db: Session, items: list[Pending]) -> None:
    """Add folders to the list (a folder already there is not repeated). The caller commits."""
    current = read_pending(db)
    known = {p.path for p in current}
    _write_pending(db, current + [i for i in items if i.path not in known])


def remove_pending(db: Session, done: list[Pending]) -> list[Pending]:
    """Take the processed folders off the list. Returns what is left (it may have grown while
    the job was running)."""
    gone = {p.path for p in done}
    left = [p for p in read_pending(db) if p.path not in gone]
    _write_pending(db, left)
    return left


def request_refresh(
    db: Session,
    settings: Settings,
    items: list[Pending],
    *,
    parent_job_id: int | None = None,
    created_by: int | None = None,
) -> engine.Job:
    """Put the folders in the list and make sure a job will scan them. The job starts
    `plex_refresh_debounce_seconds` after the first request (and not before that long has passed
    since the last scan ended); requests that come meanwhile ride along. Commits."""
    add_pending(db, items)
    debounce = float(settings.plex_refresh_debounce_seconds)
    delay = debounce
    last = get_setting(db, LAST_KEY)
    if last:
        since_last = (utcnow() - datetime.fromisoformat(last)).total_seconds()
        delay = max(debounce, debounce - since_last, 0.0)
    job = engine.enqueue(
        db,
        "plex_refresh",
        {},
        dedupe_key=JOB_KEY,
        parent_job_id=parent_job_id,
        created_by=created_by,
        delay=delay,
    )
    db.commit()
    return job


def mark_finished(db: Session) -> None:
    set_setting(db, LAST_KEY, utcnow().isoformat(timespec="seconds"))
    db.flush()


# ----------------------------------------------------------------- talking to Plex


class PlexRefused(Exception):
    """Plex refused a partial scan (bad request, unknown path...): the section is scanned."""


class PlexScanner:
    """The blocking calls to Plex that the job needs. Run them in a worker thread."""

    def __init__(self, settings: Settings, *, server_factory=None) -> None:
        self.settings = settings
        self.rules = parse_path_map(settings.library_path_map)
        self.roots = _roots(settings)
        self._factory = server_factory
        self.server, self.section = open_section(settings, server_factory)

    def plex_path(self, local: str) -> str:
        return unmap_path(local, self.rules)

    def scan_path(self, local: str) -> str:
        """Scan one folder (GET /library/sections/{id}/refresh?path=...). Returns the path sent."""
        path = self.plex_path(local)
        try:
            self.section.update(path=path)
        except (BadRequest, NotFound) as exc:
            raise PlexRefused(f"Plex refused the scan of {path} ({type(exc).__name__})") from exc
        return path

    def scan_all(self) -> None:
        self.section.update()

    def is_refreshing(self) -> bool:
        """Is the section being scanned? The flag is read again from the server each time."""
        return bool(open_section(self.settings, self._factory)[1].refreshing)

    def find_album(self, pending: Pending):
        """The Plex album that is this folder, or None. Albums with the title are listed and the
        folder of their tracks compared with ours (as Plex sees it)."""
        wanted = PurePosixPath(self.plex_path(pending.path)).as_posix().rstrip("/")
        for album in self.section.searchAlbums(title=pending.title):
            for track in album.tracks():
                for media in getattr(track, "media", None) or []:
                    for part in media.parts:
                        folder = PurePosixPath(part.file.replace("\\", "/")).parent.as_posix()
                        if folder == wanted:
                            return album
        return None

    def refresh_metadata(self, album) -> None:
        album.refresh()  # PUT /library/metadata/{ratingKey}/refresh

    def local_relative(self, plex_file: str) -> str:
        return map_path(plex_file, self.rules, self.roots)


def _roots(settings: Settings) -> list[str]:
    from app.services.library.plex_provider import _roots as roots

    return roots(settings.music_dir)


def is_transient(exc: BaseException) -> bool:
    """A problem with the connection, worth another try later."""
    return isinstance(exc, requests.exceptions.RequestException)


# ------------------------------------------------------------ the rating keys


def reconcile_keys(db: Session, scanner: PlexScanner, album, music_dir: str) -> tuple[int, int]:
    """Record the Plex rating keys on the library album and tracks of a folder that was imported
    (a library read from a folder does not get them from a scan). Returns (albums, tracks)."""
    keys: dict[str, str] = {}
    folder = None
    for track in album.tracks():
        for media in getattr(track, "media", None) or []:
            for part in media.parts:
                relative = scanner.local_relative(part.file)
                keys[relative] = str(track.ratingKey)
                folder = folder or PurePosixPath(relative).parent.as_posix()
    if folder is None:
        return 0, 0
    albums = db.scalars(
        select(LibraryAlbum).where(
            LibraryAlbum.source == SOURCE_FILESYSTEM, LibraryAlbum.path == folder
        )
    ).all()
    for row in albums:
        row.plex_rating_key = str(album.ratingKey)
    tracks = 0
    for row in db.scalars(
        select(LibraryTrack).where(
            LibraryTrack.source == SOURCE_FILESYSTEM, LibraryTrack.path.in_(list(keys))
        )
    ):
        row.plex_rating_key = keys[row.path]
        tracks += 1
    db.flush()
    return len(albums), tracks
