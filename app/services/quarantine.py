"""The quarantine folder: downloads the import refused, kept with the reason.

An administrator can look at them, take one back to be matched with MusicBrainz by hand (the
request of that download asks again), or delete what cannot be rescued. Names come from the
folder listing only and are checked before anything is moved or deleted: one path component,
a real folder, inside the quarantine.
"""

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import REQ_CANCELLED, REQ_DOWNLOADED, REQ_FAILED, DownloadRequest
from app.services import downloads as svc
from app.services.importer import library_import as lib
from app.services.library.filesystem import AUDIO_EXTENSIONS

REASON_FILE = "REASON.txt"
RESTORABLE = (REQ_FAILED, REQ_CANCELLED)


class QuarantineError(Exception):
    """Something the user asked cannot be done; `code` is an i18n error key suffix."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Entry:
    name: str  # the folder name: "12", or "12-2" when the request was refused twice
    request_id: int | None
    reason: str
    when: datetime | None
    files: list[tuple[str, int]]  # (name inside the folder, size) of the audio files
    size: int  # of everything in the folder
    request: DownloadRequest | None = None

    @property
    def restorable(self) -> bool:
        return self.request is not None and self.request.status in RESTORABLE


def _request_id(name: str) -> int | None:
    head = name.split("-", 1)[0]
    return int(head) if head.isdigit() and name.replace("-", "").isdigit() else None


def _read_reason(folder: Path) -> tuple[datetime | None, str]:
    try:
        stamp, _, reason = (folder / REASON_FILE).read_text(encoding="utf-8").partition("\n")
        return datetime.fromisoformat(stamp.strip()), reason.strip()
    except (OSError, ValueError):
        return None, ""


def _scan(folder: Path) -> tuple[list[tuple[str, int]], int]:
    audio, total = [], 0
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.is_symlink():
            size = path.stat().st_size
            total += size
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                audio.append((str(path.relative_to(folder)), size))
    return audio, total


def list_entries(settings: Settings, db: Session) -> list[Entry]:
    """The folders of the quarantine, the newest first."""
    root = lib.quarantine_root(settings)
    if not root.is_dir():
        return []
    entries = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        when, reason = _read_reason(folder)
        files, size = _scan(folder)
        request_id = _request_id(folder.name)
        entries.append(
            Entry(
                folder.name,
                request_id,
                reason,
                when or datetime.fromtimestamp(folder.stat().st_mtime),
                files,
                size,
                db.get(DownloadRequest, request_id) if request_id else None,
            )
        )
    return sorted(entries, key=lambda e: e.when or datetime.min, reverse=True)


def folder_of(settings: Settings, name: str) -> Path:
    """The folder of an entry, only if `name` is one folder directly inside the quarantine."""
    root = lib.quarantine_root(settings).resolve()
    if not name or name != Path(name).name or name in (".", ".."):
        raise QuarantineError("quarantine_bad_name")
    folder = root / name
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
        raise QuarantineError("quarantine_bad_name")
    return folder


def delete(settings: Settings, name: str) -> None:
    shutil.rmtree(folder_of(settings, name))


def restore(settings: Settings, db: Session, name: str, lowest_quality: str) -> DownloadRequest:
    """Take a folder back to the downloads folder and put its request back at the import, which
    will ask how to match the files. The caller queues the import and commits.

    The quality is not asked again: whoever takes a folder back accepts what it is."""
    folder = folder_of(settings, name)
    request_id = _request_id(name)
    request = db.get(DownloadRequest, request_id) if request_id else None
    if request is None or request.status not in RESTORABLE:
        raise QuarantineError("quarantine_no_request")
    if svc.other_active_request(db, request):
        raise QuarantineError("request_group_busy")
    root = Path(settings.downloads_dir)
    if not str(settings.downloads_dir).strip() or not root.is_dir():
        raise QuarantineError("quarantine_no_downloads")
    dest = f"{lib.DOWNLOAD_ROOT}/{request.id}"
    target = root / dest
    if target.exists():
        raise QuarantineError("quarantine_target_exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(target))
    (target / REASON_FILE).unlink(missing_ok=True)
    request.dest_dir = dest
    request.status = REQ_DOWNLOADED
    request.error = None
    request.finished_at = None
    request.quality_key = lowest_quality
    db.flush()
    return request
