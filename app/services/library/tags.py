"""Read technical info and tags from audio files with mutagen.

Tag names follow the official MusicBrainz Picard source (picard/formats/*.py). Note that
in Vorbis comments MUSICBRAINZ_TRACKID is the *recording* id (the release-track id is
MUSICBRAINZ_RELEASETRACKID), and in ID3 the recording id lives in a UFID frame.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mutagen
from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

AUDIO_EXTENSIONS = frozenset(
    {
        ".flac",
        ".mp3",
        ".m4a",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".aiff",
        ".aif",
        ".wma",
        ".ape",
        ".wv",
    }
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LEADING_INT = re.compile(r"^\s*(\d+)")
_YEAR = re.compile(r"(\d{4})")
_V0 = re.compile(r"-V\s?0\b")


class UnreadableAudio(Exception):
    """The file is not audio mutagen understands, or it is corrupt."""


@dataclass
class AudioFileInfo:
    codec: str
    lossless: bool
    bitrate: int | None = None  # kbps
    bit_depth: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    duration: float | None = None
    v0: bool = False  # LAME VBR -V0
    title: str | None = None
    artist: str | None = None
    album_artist: str | None = None
    album: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    year: int | None = None
    compilation: bool = False
    artist_sort: str | None = None
    mbid_recording: str | None = None
    mbid_artist: str | None = None
    mbid_album_artist: str | None = None
    mbid_release: str | None = None
    mbid_release_group: str | None = None


# ----------------------------------------------------------------------- helpers


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip("\x00").strip()
    return text or None


def parse_int(value: Any) -> int | None:
    """'3', '3/12', 'A3' (vinyl-style -> 3 is not guessed, so None) -> int."""
    match = _LEADING_INT.match(str(value or ""))
    return int(match.group(1)) if match else None


def parse_year(value: Any) -> int | None:
    match = _YEAR.search(str(value or ""))
    return int(match.group(1)) if match else None


def clean_mbid(value: Any) -> str | None:
    text = (clean(value) or "").lower()
    return text if _UUID_RE.match(text) else None


def is_truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _first(values: Any) -> Any:
    if isinstance(values, list | tuple):
        return values[0] if values else None
    return values


# -------------------------------------------------------------------- extractors


def _vorbis(tags: Any, info: AudioFileInfo) -> None:
    def get(key: str) -> str | None:
        return clean(_first(tags.get(key))) if tags is not None else None

    info.title = get("title")
    info.artist = get("artist")
    info.album_artist = get("albumartist") or get("album artist")
    info.album = get("album")
    info.track_no = parse_int(get("tracknumber"))
    info.disc_no = parse_int(get("discnumber"))
    info.year = parse_year(get("originaldate") or get("date") or get("year"))
    info.compilation = is_truthy(get("compilation") or "")
    info.artist_sort = get("albumartistsort") or get("artistsort")
    info.mbid_release = clean_mbid(get("musicbrainz_albumid"))
    info.mbid_release_group = clean_mbid(get("musicbrainz_releasegroupid"))
    info.mbid_artist = clean_mbid(get("musicbrainz_artistid"))
    info.mbid_album_artist = clean_mbid(get("musicbrainz_albumartistid"))
    info.mbid_recording = clean_mbid(get("musicbrainz_trackid"))  # recording id (see Picard)


def _id3(tags: Any, info: AudioFileInfo) -> None:
    if tags is None:
        return

    def text(frame_id: str) -> str | None:
        frame = tags.get(frame_id)
        return clean(frame.text[0]) if frame is not None and getattr(frame, "text", None) else None

    freetext = {}
    for frame in tags.getall("TXXX"):
        if frame.text:
            freetext[frame.desc.lower()] = clean(frame.text[0])

    info.title = text("TIT2")
    info.artist = text("TPE1")
    info.album_artist = text("TPE2")
    info.album = text("TALB")
    info.track_no = parse_int(text("TRCK"))
    info.disc_no = parse_int(text("TPOS"))
    info.year = parse_year(text("TDOR") or text("TDRC") or text("TYER"))
    info.compilation = is_truthy(text("TCMP") or "")
    info.artist_sort = text("TSO2") or text("TSOP")
    info.mbid_release = clean_mbid(freetext.get("musicbrainz album id"))
    info.mbid_release_group = clean_mbid(freetext.get("musicbrainz release group id"))
    info.mbid_artist = clean_mbid(freetext.get("musicbrainz artist id"))
    info.mbid_album_artist = clean_mbid(freetext.get("musicbrainz album artist id"))
    for frame in tags.getall("UFID"):
        if frame.owner == "http://musicbrainz.org":
            info.mbid_recording = clean_mbid(frame.data.decode("ascii", "ignore"))


def _mp4(tags: Any, info: AudioFileInfo) -> None:
    if tags is None:
        return

    def text(key: str) -> str | None:
        values = tags.get(key)
        if not values:
            return None
        value = values[0]
        if isinstance(value, bytes):
            value = value.decode("utf-8", "ignore")
        return clean(value)

    def number(key: str) -> int | None:
        values = tags.get(key)
        return parse_int(values[0][0]) if values and values[0] else None

    def free(name: str) -> str | None:
        return text(f"----:com.apple.iTunes:{name}")

    info.title = text("\xa9nam")
    info.artist = text("\xa9ART")
    info.album_artist = text("aART")
    info.album = text("\xa9alb")
    info.track_no = number("trkn")
    info.disc_no = number("disk")
    info.year = parse_year(text("\xa9day"))
    info.compilation = bool(tags.get("cpil"))
    info.artist_sort = text("soaa") or text("soar")
    info.mbid_release = clean_mbid(free("MusicBrainz Album Id"))
    info.mbid_release_group = clean_mbid(free("MusicBrainz Release Group Id"))
    info.mbid_artist = clean_mbid(free("MusicBrainz Artist Id"))
    info.mbid_album_artist = clean_mbid(free("MusicBrainz Album Artist Id"))
    info.mbid_recording = clean_mbid(free("MusicBrainz Track Id"))


# --------------------------------------------------------------------- main API


def _kbps(bitrate: Any) -> int | None:
    return int(round(bitrate / 1000)) if bitrate else None


def read_audio_file(path: Path) -> AudioFileInfo:
    """Return technical info and tags of an audio file. Raises UnreadableAudio."""
    try:
        audio = mutagen.File(path)
    except (mutagen.MutagenError, OSError, ValueError, TypeError, EOFError) as exc:
        raise UnreadableAudio(f"{type(exc).__name__}: {exc}") from exc
    if audio is None or audio.info is None:
        raise UnreadableAudio("unsupported or corrupt audio file")

    meta = audio.info
    info = AudioFileInfo(
        codec="unknown",
        lossless=False,
        bitrate=_kbps(getattr(meta, "bitrate", None)),
        sample_rate=getattr(meta, "sample_rate", None) or None,
        channels=getattr(meta, "channels", None) or None,
        duration=float(meta.length) if getattr(meta, "length", None) else None,
    )
    tags = audio.tags

    if isinstance(audio, FLAC):
        info.codec, info.lossless = "flac", True
        info.bit_depth = meta.bits_per_sample or None
        _vorbis(tags, info)
    elif isinstance(audio, OggVorbis):
        info.codec = "vorbis"
        _vorbis(tags, info)
    elif isinstance(audio, OggOpus):
        info.codec = "opus"
        _vorbis(tags, info)
    elif isinstance(audio, MP3):
        info.codec = "mp3"
        settings = str(getattr(meta, "encoder_settings", "") or "")
        info.v0 = "VBR" in str(getattr(meta, "bitrate_mode", "")) and bool(_V0.search(settings))
        _id3(tags, info)
    elif isinstance(audio, MP4):
        codec = str(getattr(meta, "codec", "") or "").lower()
        if codec.startswith("alac"):
            info.codec, info.lossless = "alac", True
            info.bit_depth = getattr(meta, "bits_per_sample", None) or None
        else:
            info.codec = "aac"
        _mp4(tags, info)
    elif isinstance(audio, WAVE):
        info.codec, info.lossless = "wav", True
        info.bit_depth = getattr(meta, "bits_per_sample", None) or None
        _id3(tags, info)
    elif isinstance(audio, AIFF):
        info.codec, info.lossless = "aiff", True
        info.bit_depth = getattr(meta, "bits_per_sample", None) or None
        _id3(tags, info)
    elif isinstance(audio, WavPack):
        info.codec, info.lossless = "wavpack", True
        info.bit_depth = getattr(meta, "bits_per_sample", None) or None
    elif isinstance(audio, MonkeysAudio):
        info.codec, info.lossless = "ape", True
        info.bit_depth = getattr(meta, "bits_per_sample", None) or None
    elif isinstance(audio, ASF):
        info.codec = "wma"
    else:
        raise UnreadableAudio(f"unsupported format: {type(audio).__name__}")
    return info
