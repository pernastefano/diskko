"""Database backups: a consistent copy of the SQLite file, made while the app keeps running.

Uses SQLite's online backup API (not a file copy, which could catch the WAL half written). Copies
live in <data_dir>/backups as diskko-YYYYmmdd-HHMMSS.db; the oldest beyond `backup_keep` go. The
copy holds everything, secrets of the settings included: treat it like the database itself.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import Settings

NAME = re.compile(r"^diskko-\d{8}-\d{6}(?:-\d+)?\.db$")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupFile:
    name: str
    size: int
    created: datetime


def _source(settings: Settings) -> Path:
    path = make_url(settings.database_url).database
    if not path:
        raise BackupError("the database is not a file")
    return Path(path)


def create_backup(settings: Settings, *, now: datetime | None = None) -> BackupFile:
    """Write a new backup and prune the old ones. Raises BackupError."""
    source = _source(settings)
    if not source.is_file():
        raise BackupError("the database file does not exist")
    folder = settings.backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    target = folder / f"diskko-{stamp}.db"
    n = 1
    while target.exists():  # two backups in the same second
        n += 1
        target = folder / f"diskko-{stamp}-{n}.db"
    partial = target.with_suffix(".partial")
    src = dst = None
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(partial)
        src.backup(dst)
        dst.close()
        dst = None
        check = sqlite3.connect(partial)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if ok != "ok":
            raise BackupError(f"the copy failed its integrity check: {ok}")
        partial.replace(target)  # appears whole or not at all
    except sqlite3.Error as exc:
        raise BackupError(f"sqlite: {exc}") from exc
    finally:
        if src is not None:
            src.close()
        if dst is not None:
            dst.close()
        partial.unlink(missing_ok=True)
    prune(settings)
    return describe(target)


def describe(path: Path) -> BackupFile:
    st = path.stat()
    return BackupFile(path.name, st.st_size, datetime.fromtimestamp(st.st_mtime, UTC))


def list_backups(settings: Settings) -> list[BackupFile]:
    """Newest first."""
    folder = settings.backup_dir
    if not folder.is_dir():
        return []
    found = [describe(p) for p in folder.iterdir() if NAME.match(p.name) and p.is_file()]
    # a "-2" suffix (second backup of one second) is newer than the plain name
    return sorted(found, key=lambda b: (b.name[7:22], len(b.name)), reverse=True)


def prune(settings: Settings) -> int:
    keep = max(1, settings.backup_keep)
    removed = 0
    for old in list_backups(settings)[keep:]:
        (settings.backup_dir / old.name).unlink(missing_ok=True)
        removed += 1
    return removed


def backup_path(settings: Settings, name: str) -> Path | None:
    """The file of a backup by name, only if it is one of ours (no path tricks)."""
    if not NAME.match(name):
        return None
    folder = settings.backup_dir.resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        return None
    return path
