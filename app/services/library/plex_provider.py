"""Plex provider: read artists, albums and tracks through plexapi (bulk listings).

Notes on what is and is not confirmed by the official documentation:
- bulk listings (`searchArtists/Albums/Tracks`) and the Track/Media/Part/AudioStream
  attributes used here are documented in python-plexapi;
- MusicBrainz ids are read from `guids` when an id starts with `mbid://` (the exact format
  is not documented, so this is best effort);
- whether audio streams (bit depth, sampling rate) are present in bulk listings is not
  documented: when they are missing, lossless files are classified as plain `lossless`.
"""

import contextlib
import posixpath
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized
from plexapi.server import PlexServer

from app.config import Settings
from app.models import SOURCE_PLEX
from app.services.library.grouping import (
    VARIOUS_ARTISTS,
    build_album,
    name_key,
    sort_name_for,
)
from app.services.library.provider import (
    LibraryError,
    LibraryProvider,
    RecordingReporter,
    ScannedArtist,
    ScannedTrack,
    ScanOutput,
    ScanReporter,
    ScanStats,
)
from app.services.library.quality import classify_track
from app.services.library.tags import clean_mbid

Reporter = ScanReporter | RecordingReporter

CONTAINER_SIZE = 1000
LOSSLESS_CODECS = frozenset({"flac", "alac", "wav", "aiff", "ape", "wavpack", "tta"})
_CODEC_ALIASES = {"ogg": "vorbis", "wv": "wavpack", "pcm_s16le": "wav", "pcm_s24le": "wav"}


# ---------------------------------------------------------------------- path mapping


def parse_path_map(raw: str) -> list[tuple[str, str]]:
    """'plex_prefix=local_prefix;...' -> rules, longest Plex prefix first."""
    rules: list[tuple[str, str]] = []
    for part in raw.replace("\n", ";").split(";"):
        plex, sep, local = part.strip().partition("=")
        if sep and plex.strip() and local.strip():
            rules.append((plex.strip().replace("\\", "/").rstrip("/"), local.strip().rstrip("/")))
    return sorted(rules, key=lambda r: len(r[0]), reverse=True)


def map_path(path: str, rules: list[tuple[str, str]], roots: list[str]) -> str:
    """Translate a path as seen by Plex to a local one, relative to the library root if inside."""
    normalized = path.replace("\\", "/")
    for plex_prefix, local_prefix in rules:
        if normalized == plex_prefix or normalized.startswith(plex_prefix + "/"):
            normalized = local_prefix + normalized[len(plex_prefix) :]
            break
    for root in roots:
        root = root.rstrip("/")
        if root and normalized.startswith(root + "/"):
            return normalized[len(root) + 1 :]
    return normalized


def _roots(music_dir: Path) -> list[str]:
    roots = [music_dir.as_posix()]
    with contextlib.suppress(OSError):
        roots.append(music_dir.resolve().as_posix())
    return roots


# ------------------------------------------------------------------------- helpers


def mbid_from_guids(item: Any) -> str | None:
    for guid in getattr(item, "guids", None) or []:
        value = str(getattr(guid, "id", "") or "")
        if value.startswith("mbid://"):
            mbid = clean_mbid(value.removeprefix("mbid://"))
            if mbid:
                return mbid
    return None


def _codec(raw: str | None) -> str | None:
    if not raw:
        return None
    code = raw.lower()
    return _CODEC_ALIASES.get(code, code)


def _audio_stream(part: Any) -> Any | None:
    try:
        streams = part.audioStreams()
    except Exception:
        return None
    return streams[0] if streams else None


def _timestamp_ns(value: Any) -> int:
    try:
        return int(value.timestamp() * 1_000_000_000)
    except (AttributeError, OSError, ValueError):
        return 0


class PlexProvider(LibraryProvider):
    source = SOURCE_PLEX

    def __init__(self, settings: Settings, *, server_factory=None, timeout: int = 30) -> None:
        self.settings = settings
        self._server_factory = server_factory or (
            lambda url, token: PlexServer(url, token, timeout=timeout)
        )
        self.rules = parse_path_map(settings.library_path_map)
        self.roots = _roots(settings.music_dir)

    # ---------------------------------------------------------------- connection

    def _connect(self):
        url, token = self.settings.plex_url.strip(), self.settings.plex_token.strip()
        if not url or not token:
            raise LibraryError("Plex is not configured: set PLEX_URL and PLEX_TOKEN")
        try:
            server = self._server_factory(url, token)
            section = server.library.section(self.settings.plex_music_section)
        except Unauthorized as exc:
            raise LibraryError("Plex rejected the token (unauthorized)") from exc
        except NotFound as exc:
            raise LibraryError(
                f"Plex library section {self.settings.plex_music_section!r} not found"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise LibraryError(f"Cannot connect to Plex ({type(exc).__name__})") from exc
        except PlexApiException as exc:
            raise LibraryError(f"Plex error ({type(exc).__name__})") from exc
        if getattr(section, "type", None) != "artist":
            raise LibraryError(
                f"Plex section {self.settings.plex_music_section!r} is not a music library"
            )
        return section

    @staticmethod
    def _fetch(label: str, call) -> list:
        try:
            return list(call(container_size=CONTAINER_SIZE))
        except Unauthorized as exc:
            raise LibraryError("Plex rejected the token (unauthorized)") from exc
        except requests.exceptions.RequestException as exc:
            raise LibraryError(f"Lost connection to Plex while reading {label}") from exc
        except PlexApiException as exc:
            raise LibraryError(f"Plex error while reading {label} ({type(exc).__name__})") from exc

    # --------------------------------------------------------------------- scan

    def scan(
        self,
        known: dict[str, ScannedTrack],
        reporter: Reporter,
        *,
        force: bool = False,
    ) -> ScanOutput:
        reporter.progress(0, "Connecting to Plex")
        section = self._connect()
        reporter.checkpoint()
        reporter.progress(5, "Reading artists from Plex")
        artists = self._fetch("artists", section.searchArtists)
        reporter.checkpoint()
        reporter.progress(15, "Reading albums from Plex")
        albums = self._fetch("albums", section.searchAlbums)
        reporter.checkpoint()
        reporter.progress(25, "Reading tracks from Plex")
        tracks = self._fetch("tracks", section.searchTracks)
        reporter.log(
            f"Plex returned {len(artists)} artist(s), {len(albums)} album(s), "
            f"{len(tracks)} track(s)"
        )

        stats = ScanStats(files_seen=len(tracks))
        by_album: dict[str, list[ScannedTrack]] = defaultdict(list)
        for i, plex_track in enumerate(tracks):
            if i % 500 == 0:
                reporter.checkpoint()
                reporter.progress(
                    25 + 35 * i / max(len(tracks), 1), f"Processing tracks {i}/{len(tracks)}"
                )
            scanned = self._convert_track(plex_track)
            if scanned is None:
                stats.errors += 1
                continue
            old = known.get(scanned.path)
            if (
                old
                and not force
                and (old.file_size, old.file_mtime_ns)
                == (
                    scanned.file_size,
                    scanned.file_mtime_ns,
                )
            ):
                stats.files_skipped += 1
            else:
                stats.files_read += 1
            by_album[str(plex_track.parentRatingKey)].append(scanned)

        reporter.progress(60, "Grouping albums")
        albums_by_artist: dict[str, list] = defaultdict(list)
        for album in albums:
            albums_by_artist[str(album.parentRatingKey)].append(album)

        result: list[ScannedArtist] = []
        for artist in artists:
            built = []
            for album in albums_by_artist.get(str(artist.ratingKey), []):
                members = by_album.get(str(album.ratingKey))
                if members:
                    built.append(self._convert_album(album, members, artist.title))
            if built:
                title = artist.title or "Unknown Artist"
                result.append(
                    ScannedArtist(
                        name=title,
                        sort_name=getattr(artist, "titleSort", None) or sort_name_for(title),
                        mbid=mbid_from_guids(artist),
                        albums=built,
                    )
                )
        result.sort(key=lambda a: name_key(a.sort_name))
        return ScanOutput(artists=result, stats=stats)

    # -------------------------------------------------------------- conversion

    def _convert_track(self, track: Any) -> ScannedTrack | None:
        try:
            media = track.media[0] if track.media else None
            part = media.parts[0] if media is not None and media.parts else None
            raw_path = getattr(part, "file", None)
            path = (
                map_path(raw_path, self.rules, self.roots)
                if raw_path
                else f"plex:track:{track.ratingKey}"
            )
            codec = _codec(getattr(media, "audioCodec", None))
            lossless = codec in LOSSLESS_CODECS
            stream = _audio_stream(part) if part is not None else None
            bit_depth = getattr(stream, "bitDepth", None)
            bitrate = getattr(media, "bitrate", None)
            duration_ms = getattr(track, "duration", None)
            return ScannedTrack(
                path=path,
                file_size=int(getattr(part, "size", 0) or 0),
                file_mtime_ns=_timestamp_ns(getattr(track, "updatedAt", None)),
                title=track.title or "",
                track_no=getattr(track, "index", None),
                disc_no=getattr(track, "parentIndex", None),
                codec=codec,
                lossless=lossless,
                bitrate=bitrate,
                bit_depth=bit_depth,
                sample_rate=getattr(stream, "samplingRate", None),
                duration=duration_ms / 1000 if duration_ms else None,
                quality_tier=classify_track(
                    lossless=lossless, bitrate_kbps=bitrate, bit_depth=bit_depth
                ),
                artist=getattr(track, "grandparentTitle", None),
                album_artist=getattr(track, "grandparentTitle", None),
                album_title=getattr(track, "parentTitle", None),
                year=getattr(track, "year", None) or getattr(track, "parentYear", None),
                mbid_recording=mbid_from_guids(track),
                plex_rating_key=str(track.ratingKey),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            return None

    def _convert_album(self, album: Any, members: list[ScannedTrack], artist_title: str):
        fallback = f"plex:album:{album.ratingKey}"
        paths = [t.path for t in members]
        if any(p.startswith("plex:") for p in paths):
            path = fallback
        else:
            try:  # common folder of all tracks (multi-disc albums share the album folder)
                path = posixpath.commonpath(sorted({posixpath.dirname(p) for p in paths}))
            except ValueError:  # mix of absolute and relative paths
                path = fallback
        built = build_album(path, members)
        built.title = album.title or built.title
        built.year = getattr(album, "year", None) or built.year
        built.mbid_release = mbid_from_guids(album) or built.mbid_release
        built.plex_rating_key = str(album.ratingKey)
        built.is_compilation = name_key(artist_title or "") == name_key(VARIOUS_ARTISTS)
        return built
