"""Everything that can be read about the file of a library track: the file, the audio, the tags.

Read with mutagen when the file is reachable; what the library already knows (the scan's
numbers) when it is not (a track Plex knows only by address, a folder that is not mounted).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mutagen

from app.models import LibraryTrack
from app.services.albums import resolve_library_file

MAX_TAGS = 80
MAX_VALUE = 300
_BITRATE_MODES = {"CBR": "constant (CBR)", "VBR": "variable (VBR)", "ABR": "average (ABR)"}


@dataclass
class TrackDetails:
    # (section, [(label key, value)]): the labels are translated by the template
    sections: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)
    tags: list[tuple[str, str]] = field(default_factory=list)  # every tag as the file has it
    readable: bool = False  # the file was opened (False: only what the library knows)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def human_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = round(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _codec_name(audio: Any, extension: str) -> str:
    info = audio.info
    kind = type(audio).__name__
    if kind == "MP3":
        version = getattr(info, "version", None)
        layer = getattr(info, "layer", 3)
        return f"MP3 (MPEG-{version:g} Layer {'I' * layer if layer < 4 else layer})".replace(
            "Layer IIII", "Layer IV"
        )
    if kind == "MP4":
        codec = getattr(info, "codec", "") or ""
        label = "ALAC (Apple Lossless)" if codec == "alac" else "AAC"
        return f"{label} ({codec})" if codec else label
    names = {
        "FLAC": "FLAC",
        "OggVorbis": "Ogg Vorbis",
        "OggOpus": "Opus",
        "WAVE": "WAV",
        "AIFF": "AIFF",
        "WavPack": "WavPack",
        "MonkeysAudio": "Monkey's Audio (APE)",
        "ASF": "WMA",
    }
    return names.get(kind, kind or extension.upper())


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(t for v in value if (t := _text(v)))
    return str(value).strip()


def _tags_of(audio: Any) -> tuple[list[tuple[str, str]], bool, int]:
    """([(name, value)], has embedded cover, cover bytes). Binary values are left out."""
    tags = getattr(audio, "tags", None)
    items: list[tuple[str, str]] = []
    cover_bytes = 0
    if tags is None:
        return items, False, 0
    pictures = getattr(audio, "pictures", None) or []
    cover_bytes = sum(len(p.data) for p in pictures)
    if hasattr(tags, "getall"):  # ID3: frames
        for frame in tags.values():
            if frame.FrameID == "APIC":
                cover_bytes += len(getattr(frame, "data", b""))
                continue
            value = _text(getattr(frame, "text", None) or getattr(frame, "url", None) or "")
            if value:
                name = frame.FrameID
                if desc := getattr(frame, "desc", ""):
                    name = f"{name}:{desc}"
                items.append((name, value))
    else:
        try:
            pairs = list(tags.items())
        except (AttributeError, TypeError):
            pairs = []
        for key, raw in pairs:
            if key == "covr":
                cover_bytes += sum(len(bytes(c)) for c in raw)
                continue
            value = _text(raw)
            if value:
                items.append((str(key), value))
    return items, cover_bytes > 0, cover_bytes


def _replaygain(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    names = {
        "replaygain_track_gain": "rg_track_gain",
        "replaygain_track_peak": "rg_track_peak",
        "replaygain_album_gain": "rg_album_gain",
        "replaygain_album_peak": "rg_album_peak",
    }
    found = []
    for key, value in items:
        simple = key.lower().replace("txxx:", "").replace("----:com.apple.itunes:", "")
        if simple in names:
            found.append((names[simple], value))
    return found


def _first(items: list[tuple[str, str]], *keys: str) -> str:
    wanted = {k.lower() for k in keys}
    for key, value in items:
        if key.lower() in wanted:
            return value
    return ""


def describe(
    track: LibraryTrack, music_dir: Path, *, quality_name: str = "", show_path: bool = False
) -> TrackDetails:
    path = resolve_library_file(music_dir, track.path)
    details = TrackDetails()
    file_rows: list[tuple[str, str]] = []
    audio_rows: list[tuple[str, str]] = []
    tag_rows: list[tuple[str, str]] = []
    audio = None
    if path is not None:
        try:
            audio = mutagen.File(path)
        except (mutagen.MutagenError, OSError):
            audio = None
        stat = path.stat()
        file_rows.append(("filename", path.name))
        if show_path:
            file_rows.append(("path", track.path))
        file_rows.append(("size", f"{human_size(stat.st_size)} ({stat.st_size:,} bytes)"))
        modified = datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%d %H:%M UTC")
        file_rows.append(("modified", modified))
        file_rows.append(("extension", path.suffix.lstrip(".").upper()))
    else:
        file_rows.append(
            ("filename", Path(track.path).name if not track.path.startswith("plex:") else "")
        )
        if track.file_size:
            file_rows.append(("size", f"{human_size(track.file_size)} ({track.file_size:,} bytes)"))

    if audio is not None and getattr(audio, "info", None) is not None:
        details.readable = True
        info = audio.info
        audio_rows.append(("codec", _codec_name(audio, path.suffix if path else "")))
        audio_rows.append(("lossless", "yes" if track.lossless else "no"))
        bitrate = getattr(info, "bitrate", None)
        if bitrate:
            audio_rows.append(("bitrate", f"{round(bitrate / 1000)} kbps"))
        mode = getattr(info, "bitrate_mode", None)
        if mode is not None and (name := getattr(mode, "name", "")) in _BITRATE_MODES:
            audio_rows.append(("bitrate_mode", _BITRATE_MODES[name]))
        if rate := getattr(info, "sample_rate", None):
            audio_rows.append(("sample_rate", f"{rate:,} Hz ({rate / 1000:g} kHz)"))
        if depth := getattr(info, "bits_per_sample", None):
            audio_rows.append(("bit_depth", f"{depth} bit"))
        if channels := getattr(info, "channels", None):
            layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels} channels")
            audio_rows.append(("channels", f"{channels} ({layout})"))
        if length := getattr(info, "length", None):
            audio_rows.append(("duration", f"{human_duration(length)} ({length:.2f} s)"))
        if encoder := getattr(info, "encoder_info", "") or getattr(info, "encoder", ""):
            audio_rows.append(("encoder", str(encoder)))
        if md5 := getattr(info, "md5_signature", 0):
            audio_rows.append(("md5", f"{md5:032x}"))
        vendor = getattr(getattr(audio, "tags", None), "vendor", None)
        if vendor:
            audio_rows.append(("vendor", str(vendor)))
        items, has_cover, cover_bytes = _tags_of(audio)
        details.tags = [(k, v[:MAX_VALUE]) for k, v in items[:MAX_TAGS]]
        audio_rows.extend(_replaygain(items))
        audio_rows.append(
            ("cover", f"embedded ({human_size(cover_bytes)})" if has_cover else "none")
        )
        for key, names in (
            ("title", ("title", "TIT2", "©nam")),
            ("artist", ("artist", "TPE1", "©ART")),
            ("album_artist", ("albumartist", "TPE2", "aART")),
            ("album", ("album", "TALB", "©alb")),
            ("track", ("tracknumber", "TRCK", "trkn")),
            ("disc", ("discnumber", "TPOS", "disk")),
            ("date", ("date", "year", "TDRC", "©day")),
            ("genre", ("genre", "TCON", "©gen")),
            ("musicbrainz_recording", ("musicbrainz_trackid", "UFID:http://musicbrainz.org")),
            ("musicbrainz_release", ("musicbrainz_albumid", "TXXX:MusicBrainz Album Id")),
            (
                "musicbrainz_release_group",
                ("musicbrainz_releasegroupid", "TXXX:MusicBrainz Release Group Id"),
            ),
            ("musicbrainz_artist", ("musicbrainz_artistid", "TXXX:MusicBrainz Artist Id")),
        ):
            if value := _first(items, *names):
                tag_rows.append((key, value[:MAX_VALUE]))
    else:
        # only what the scan stored
        audio_rows.append(("codec", (track.codec or "").upper()))
        audio_rows.append(("lossless", "yes" if track.lossless else "no"))
        if track.bitrate:
            audio_rows.append(("bitrate", f"{track.bitrate} kbps"))
        if track.sample_rate:
            audio_rows.append(
                ("sample_rate", f"{track.sample_rate:,} Hz ({track.sample_rate / 1000:g} kHz)")
            )
        if track.bit_depth:
            audio_rows.append(("bit_depth", f"{track.bit_depth} bit"))
        if track.duration:
            audio_rows.append(
                ("duration", f"{human_duration(track.duration)} ({track.duration:.2f} s)")
            )
        tag_rows = [
            (k, v)
            for k, v in (
                ("title", track.title),
                ("artist", track.artist or ""),
                ("album", track.album_title or ""),
                ("date", str(track.year or "")),
            )
            if v
        ]
    if quality_name:
        audio_rows.append(("quality", quality_name))
    details.sections = [
        s for s in (("file", file_rows), ("audio", audio_rows), ("tags", tag_rows)) if s[1]
    ]
    return details
