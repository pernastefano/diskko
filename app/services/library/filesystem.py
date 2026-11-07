"""Filesystem provider: walk the music folder, read tags with mutagen, scan incrementally."""

import os
import re
from collections.abc import Callable
from pathlib import Path

from app.models import SOURCE_FILESYSTEM
from app.quality import DEFAULT_PROFILES, ProfileSet
from app.services.library.grouping import group_tracks
from app.services.library.provider import (
    LibraryError,
    LibraryProvider,
    RecordingReporter,
    ScannedTrack,
    ScanOutput,
    ScanReporter,
    ScanStats,
)
from app.services.library.tags import (
    AUDIO_EXTENSIONS,
    AudioFileInfo,
    UnreadableAudio,
    read_audio_file,
)

# Folders created by NAS/OS software, never music.
IGNORED_DIRS = frozenset(
    {"@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information", "lost+found"}
)
MAX_LOGGED_WARNINGS = 50
_LEADING_NUMBER = re.compile(r"^\s*(\d{1,3})\s*[-._ )]+\s*")

Reporter = ScanReporter | RecordingReporter


def _is_ignored_dir(name: str) -> bool:
    return name.startswith((".", "@")) or name in IGNORED_DIRS


def _fallback_title(rel_path: str) -> tuple[str, int | None]:
    """Title and track number guessed from a file name like '03 - Song.flac'."""
    stem = Path(rel_path).stem
    match = _LEADING_NUMBER.match(stem)
    if match and stem[match.end() :].strip():
        return stem[match.end() :].strip(), int(match.group(1))
    return stem, None


def to_scanned_track(
    rel_path: str, size: int, mtime_ns: int, info: AudioFileInfo, profiles: ProfileSet
) -> ScannedTrack:
    fallback_title, fallback_no = _fallback_title(rel_path)
    return ScannedTrack(
        path=rel_path,
        file_size=size,
        file_mtime_ns=mtime_ns,
        title=info.title or fallback_title,
        track_no=info.track_no or fallback_no,
        disc_no=info.disc_no,
        codec=info.codec,
        lossless=info.lossless,
        v0=info.v0,
        bitrate=info.bitrate,
        bit_depth=info.bit_depth,
        sample_rate=info.sample_rate,
        duration=info.duration,
        quality_tier=profiles.classify(
            lossless=info.lossless,
            bitrate_kbps=info.bitrate,
            bit_depth=info.bit_depth,
            sample_rate=info.sample_rate,
            v0=info.v0,
        ),
        artist=info.artist,
        album_artist=info.album_artist,
        album_title=info.album,
        artist_sort=info.artist_sort,
        year=info.year,
        compilation=info.compilation,
        mbid_recording=info.mbid_recording,
        mbid_artist=info.mbid_artist,
        mbid_album_artist=info.mbid_album_artist,
        mbid_release=info.mbid_release,
        mbid_release_group=info.mbid_release_group,
    )


class FilesystemProvider(LibraryProvider):
    source = SOURCE_FILESYSTEM

    def __init__(
        self,
        root: Path,
        profiles: ProfileSet = DEFAULT_PROFILES,
        *,
        reader: Callable[[Path], AudioFileInfo] = read_audio_file,
    ) -> None:
        self.root = Path(root)
        self.profiles = profiles
        self._read = reader

    # ---------------------------------------------------------------- discovery

    def _discover(self, reporter: Reporter) -> list[tuple[str, int, int]]:
        """(relative path, size, mtime_ns) of every audio file, using stat() only."""
        if not self.root.is_dir():
            raise LibraryError(f"Library folder not found or not a directory: {self.root}")
        root_real = self.root.resolve()
        found: list[tuple[str, int, int]] = []
        unsafe = 0
        for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not _is_ignored_dir(d))
            reporter.checkpoint()
            for name in sorted(filenames):
                if name.startswith(".") or Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                full = Path(dirpath) / name
                try:
                    # Symlinked files are only accepted when they stay inside the library.
                    if not full.resolve().is_relative_to(root_real):
                        unsafe += 1
                        continue
                    st = full.stat()
                except OSError:
                    continue
                if not full.is_file():
                    continue
                found.append((full.relative_to(root_real).as_posix(), st.st_size, st.st_mtime_ns))
        if unsafe:
            reporter.log(f"Ignored {unsafe} file(s) linking outside the library folder", "warning")
        return found

    # ------------------------------------------------------------------- scan

    def scan(
        self,
        known: dict[str, ScannedTrack],
        reporter: Reporter,
        *,
        force: bool = False,
    ) -> ScanOutput:
        reporter.progress(0, "Looking for audio files")
        files = self._discover(reporter)
        stats = ScanStats(files_seen=len(files))

        tracks: list[ScannedTrack] = []
        to_read: list[tuple[str, int, int]] = []
        for rel, size, mtime_ns in files:
            old = known.get(rel)
            if old and not force and old.file_size == size and old.file_mtime_ns == mtime_ns:
                tracks.append(old)
                stats.files_skipped += 1
            else:
                to_read.append((rel, size, mtime_ns))
        reporter.log(
            f"Found {len(files)} audio file(s): {len(to_read)} to read, "
            f"{stats.files_skipped} unchanged"
        )

        warnings = 0
        for i, (rel, size, mtime_ns) in enumerate(to_read, start=1):
            reporter.checkpoint()
            reporter.progress(60 * i / len(to_read), f"Reading tags {i}/{len(to_read)}")
            try:
                info = self._read(self.root.resolve() / rel)
            except UnreadableAudio as exc:
                stats.errors += 1
                if warnings < MAX_LOGGED_WARNINGS:
                    reporter.log(f"Cannot read {rel}: {exc}", "warning")
                    warnings += 1
                if rel in known:  # keep what we knew instead of dropping the track
                    tracks.append(known[rel])
                continue
            stats.files_read += 1
            tracks.append(to_scanned_track(rel, size, mtime_ns, info, self.profiles))
        if stats.errors > warnings:
            reporter.log(f"{stats.errors - warnings} more unreadable file(s) not listed", "warning")

        reporter.progress(60, "Grouping albums")
        return ScanOutput(artists=group_tracks(tracks, self.profiles), stats=stats)
