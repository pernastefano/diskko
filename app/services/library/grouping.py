"""Group scanned tracks into albums and artists (pure functions, no I/O)."""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from pathlib import PurePosixPath

from app.quality import ProfileSet, dominant
from app.services.library.provider import ScannedAlbum, ScannedArtist, ScannedTrack

VARIOUS_ARTISTS = "Various Artists"
UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"
_VARIOUS_NAMES = {"various artists", "various", "va", "v a", "varios artistas"}
# 'CD1', 'Disc 2', 'disk-3', 'CD 1 - Bonus' ...
_DISC_DIR = re.compile(r"^(cd|disc|disk|dvd)\s*[-_. ]?\s*\d+\b", re.IGNORECASE)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def name_key(name: str) -> str:
    """Normalize a name for comparison: case, accents, punctuation and spacing."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    key = _NON_WORD.sub(" ", text).strip()
    return key or name.strip().casefold()


def sort_name_for(name: str) -> str:
    """'The Beatles' -> 'Beatles, The'."""
    if name.lower().startswith("the ") and len(name) > 4:
        return f"{name[4:]}, The"
    return name


def album_dir_of(path: str) -> str:
    """Folder of the album: the track folder, or its parent for 'CD1'/'Disc 2' folders."""
    parent = PurePosixPath(path).parent
    if parent.name and _DISC_DIR.match(parent.name):
        parent = parent.parent
    return "" if str(parent) == "." else str(parent)


def _most_common(values: Iterable):
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


def _is_various(name: str | None) -> bool:
    return bool(name) and name_key(name) in _VARIOUS_NAMES


def _resolve_artist(adir: str, tracks: list[ScannedTrack]) -> tuple[str, str | None, str, bool]:
    """(name, mbid, sort_name, is_compilation) of an album."""
    flagged = sum(1 for t in tracks if t.compilation) * 2 > len(tracks)
    album_artist = _most_common(t.album_artist for t in tracks)
    if flagged or _is_various(album_artist):
        return VARIOUS_ARTISTS, None, VARIOUS_ARTISTS, True

    name = album_artist or _most_common(t.artist for t in tracks)
    if not name:
        segments = adir.split("/") if adir else []
        name = segments[-2] if len(segments) >= 2 else UNKNOWN_ARTIST
    mbid = _most_common(t.mbid_album_artist for t in tracks)
    if mbid is None and not album_artist:
        mbid = _most_common(t.mbid_artist for t in tracks)
    sort = _most_common(t.artist_sort for t in tracks) or sort_name_for(name)
    return name, mbid, sort, False


def build_album(adir: str, tracks: list[ScannedTrack], profiles: ProfileSet) -> ScannedAlbum:
    fallback = adir.split("/")[-1] if adir else UNKNOWN_ALBUM
    bitrates = [t.bitrate for t in tracks if t.bitrate]
    return ScannedAlbum(
        path=adir,
        title=_most_common(t.album_title for t in tracks) or fallback,
        year=_most_common(t.year for t in tracks),
        format=dominant(t.codec for t in tracks),
        quality_tier=profiles.album_quality(t.quality_tier for t in tracks),
        bitrate=round(sum(bitrates) / len(bitrates)) if bitrates else None,
        disc_count=max([t.disc_no or 1 for t in tracks] + [1]),
        total_size=sum(t.file_size for t in tracks),
        is_compilation=False,  # set by the caller
        mbid_release=_most_common(t.mbid_release for t in tracks),
        mbid_release_group=_most_common(t.mbid_release_group for t in tracks),
        plex_rating_key=_most_common(t.plex_rating_key for t in tracks),
        tracks=sorted(tracks, key=lambda t: (t.disc_no or 1, t.track_no or 0, t.path)),
    )


def group_tracks(tracks: Iterable[ScannedTrack], profiles: ProfileSet) -> list[ScannedArtist]:
    """Albums are (album folder, album title); artists are merged by MBID or by name."""
    buckets: dict[tuple[str, str], list[ScannedTrack]] = {}
    for track in tracks:
        adir = album_dir_of(track.path)
        title = track.album_title or (adir.split("/")[-1] if adir else UNKNOWN_ALBUM)
        buckets.setdefault((adir, name_key(title)), []).append(track)

    artists: dict[str, ScannedArtist] = {}
    for (adir, _), members in buckets.items():
        name, mbid, sort, compilation = _resolve_artist(adir, members)
        album = build_album(adir, members, profiles)
        album.is_compilation = compilation
        key = f"mbid:{mbid}" if mbid else f"name:{name_key(name)}"
        artist = artists.setdefault(key, ScannedArtist(name=name, sort_name=sort, mbid=mbid))
        artist.albums.append(album)

    # An artist seen both with and without MBID (partially tagged library) is one artist.
    by_name: dict[str, list[str]] = {}
    for key, artist in artists.items():
        if artist.mbid:
            by_name.setdefault(name_key(artist.name), []).append(key)
    for key in [k for k in artists if k.startswith("name:")]:
        matches = by_name.get(key.removeprefix("name:"), [])
        if len(matches) == 1:
            artists[matches[0]].albums.extend(artists.pop(key).albums)

    result = sorted(artists.values(), key=lambda a: (name_key(a.sort_name), a.mbid or ""))
    for artist in result:
        artist.albums.sort(key=lambda a: (a.year or 0, name_key(a.title), a.path))
    return result
