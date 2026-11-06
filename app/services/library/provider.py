"""The LibraryProvider interface and the data it produces."""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.jobs import engine
from app.jobs.errors import JobCancelled


class LibraryError(Exception):
    """A problem with the library itself (missing path, bad configuration, unsafe scan)."""


@dataclass
class ScannedTrack:
    path: str  # relative to the library root, POSIX style
    file_size: int = 0
    file_mtime_ns: int = 0
    title: str = ""
    track_no: int | None = None
    disc_no: int | None = None
    codec: str | None = None
    lossless: bool = False
    bitrate: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    duration: float | None = None
    quality_tier: str = "any"
    artist: str | None = None
    album_artist: str | None = None
    album_title: str | None = None
    artist_sort: str | None = None
    year: int | None = None
    compilation: bool = False
    mbid_recording: str | None = None
    mbid_artist: str | None = None
    mbid_album_artist: str | None = None
    mbid_release: str | None = None
    mbid_release_group: str | None = None
    plex_rating_key: str | None = None


@dataclass
class ScannedAlbum:
    path: str
    title: str
    year: int | None
    format: str | None
    quality_tier: str
    bitrate: int | None
    disc_count: int
    total_size: int
    is_compilation: bool
    mbid_release: str | None
    mbid_release_group: str | None
    plex_rating_key: str | None
    tracks: list[ScannedTrack] = field(default_factory=list)


@dataclass
class ScannedArtist:
    name: str
    sort_name: str
    mbid: str | None
    albums: list[ScannedAlbum] = field(default_factory=list)


@dataclass
class ScanStats:
    files_seen: int = 0
    files_read: int = 0
    files_skipped: int = 0  # unchanged since the last scan: not opened
    errors: int = 0


@dataclass
class ScanOutput:
    artists: list[ScannedArtist]
    stats: ScanStats


class ScanReporter:
    """Progress, logging and cooperative cancellation for code running in a thread.

    Every call uses its own short DB transaction, so it is safe from a worker thread.
    """

    def __init__(
        self, factory: sessionmaker[Session], job_id: int, *, cancel_interval: float = 0.2
    ) -> None:
        self._factory = factory
        self._job_id = job_id
        self._cancel_interval = cancel_interval
        self._last_check = 0.0
        self._last_progress: tuple[int, str | None] | None = None

    def progress(self, pct: float, step: str | None = None) -> None:
        key = (int(pct), step)
        if key == self._last_progress:
            return
        self._last_progress = key
        with self._factory() as db:
            engine.update_progress(db, self._job_id, pct, step)

    def log(self, message: str, level: str = "info") -> None:
        with self._factory() as db:
            engine.add_log(db, self._job_id, level, message)

    def checkpoint(self) -> None:
        now = time.monotonic()
        if now - self._last_check < self._cancel_interval:
            return
        self._last_check = now
        with self._factory() as db:
            if engine.is_cancel_requested(db, self._job_id):
                raise JobCancelled


class RecordingReporter:
    """In-memory reporter for tests and scripts. `cancel_after` cancels at that checkpoint."""

    def __init__(self, cancel_after: int | None = None) -> None:
        self.progress_calls: list[tuple[float, str | None]] = []
        self.logs: list[tuple[str, str]] = []
        self.checkpoints = 0
        self._cancel_after = cancel_after

    def progress(self, pct: float, step: str | None = None) -> None:
        self.progress_calls.append((pct, step))

    def log(self, message: str, level: str = "info") -> None:
        self.logs.append((level, message))

    def checkpoint(self) -> None:
        self.checkpoints += 1
        if self._cancel_after is not None and self.checkpoints > self._cancel_after:
            raise JobCancelled

    def messages(self, level: str | None = None) -> list[str]:
        return [m for lv, m in self.logs if level is None or lv == level]


class LibraryProvider(ABC):
    """A source of library content (filesystem, Plex).

    `scan` is blocking and runs in a worker thread. `known` maps the relative path of
    every track already in the DB for this source to its stored data, so that providers
    can skip unchanged items (incremental scan).
    """

    source: str

    @abstractmethod
    def scan(
        self,
        known: dict[str, ScannedTrack],
        reporter: ScanReporter | RecordingReporter,
        *,
        force: bool = False,
    ) -> ScanOutput: ...


def track_fields(track: ScannedTrack) -> dict[str, Any]:
    """Columns of LibraryTrack that come from a ScannedTrack."""
    data = dict(vars(track))
    data.pop("path")
    return data
