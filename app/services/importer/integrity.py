"""Is what was downloaded a complete, readable album?"""

from dataclasses import dataclass
from pathlib import Path

from app.services.library.filesystem import AUDIO_EXTENSIONS
from app.services.library.tags import AudioFileInfo, UnreadableAudio, read_audio_file

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MIN_DURATION_S = 0.5  # shorter than this is a broken file, not a track


class ImportRejected(Exception):
    """The download cannot be imported: the reason goes to the user and into the quarantine."""


@dataclass
class DownloadedFile:
    path: Path
    relative: str  # inside the download folder (the disc folder, if any, is its first part)
    info: AudioFileInfo
    size: int


def find_files(root: Path) -> tuple[list[Path], list[Path]]:
    """(audio files, image files) under `root`, sorted. Links that leave `root` are refused."""
    real = root.resolve()
    audio: list[Path] = []
    images: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.name.startswith(".") or not path.is_file():
            continue
        if not path.resolve().is_relative_to(real):
            raise ImportRejected(f"{path.name} links outside the download folder")
        suffix = path.suffix.lower()
        if suffix in AUDIO_EXTENSIONS:
            audio.append(path)
        elif suffix in IMAGE_EXTENSIONS:
            images.append(path)
    return audio, images


def check_files(root: Path, audio: list[Path]) -> list[DownloadedFile]:
    """Every audio file must open and have a duration. Raises ImportRejected listing the bad
    ones (all of them, not only the first)."""
    good: list[DownloadedFile] = []
    bad: list[str] = []
    for path in audio:
        relative = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
            if size <= 0:
                raise UnreadableAudio("empty file")
            info = read_audio_file(path)
            if not info.duration or info.duration < MIN_DURATION_S:
                raise UnreadableAudio("no audio (zero duration)")
        except (UnreadableAudio, OSError) as exc:
            bad.append(f"{relative}: {exc}")
            continue
        good.append(DownloadedFile(path, relative, info, size))
    if bad:
        shown = "; ".join(bad[:5]) + (f"; and {len(bad) - 5} more" if len(bad) > 5 else "")
        raise ImportRejected(f"{len(bad)} file(s) are damaged or unreadable: {shown}")
    if not good:
        raise ImportRejected("the download has no audio files")
    return good


def pick_cover_file(images: list[Path]) -> Path | None:
    """An image in the download that looks like the front cover (the biggest of the named ones,
    else the biggest image), if it is a sensible size."""

    def size(p: Path) -> int:
        return p.stat().st_size

    usable = [p for p in images if 0 < size(p) <= MAX_IMAGE_BYTES]
    named = [p for p in usable if p.stem.lower() in COVER_NAMES]
    pool = named or usable
    return max(pool, key=size) if pool else None
