"""What the album page shows: the tracks, and for each one something to play (if there is any).

* Owned album: the tracks the scan found, each played from its file on disk.
* Album not owned: the tracks MusicBrainz lists, each with the 30 second preview of iTunes or
  Deezer when one matches. Without a MusicBrainz list the preview tracks are shown instead.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    MATCH_MANUAL,
    MATCH_MATCHED,
    MATCH_TENTATIVE,
    Artist,
    LibraryAlbum,
    LibraryTrack,
    PreviewTrack,
    ReleaseTrack,
)
from app.services.library.grouping import name_key

OWNED_STATUSES = (MATCH_MATCHED, MATCH_TENTATIVE, MATCH_MANUAL)

# What a browser can play. Anything else (APE, WMA, WavPack...) is listed but not played: there
# is no conversion.
PLAYABLE = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "aiff": "audio/aiff",
    "aif": "audio/aiff",
}
TITLE_MATCH = 0.85  # preview title against the track title, when the positions differ
POSITION_MATCH = 0.6  # ... and when the position agrees


def extension_of(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def media_type_of(extension: str) -> str | None:
    return PLAYABLE.get(extension.lower())


def resolve_library_file(music_dir: Path, relative: str) -> Path | None:
    """The file of a track, only if it really lies inside the library folder.

    The path is resolved (links followed) and must stay under the library root: a path that
    climbs out of it, or a link that points outside, is refused. Tracks that Plex knows only by
    an address (not a file we can reach) have no file either.
    """
    if not relative or relative.startswith("plex:"):
        return None
    root = Path(music_dir).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


@dataclass
class ArtistRef:
    """Who plays a track: the name, our artist page when we have one, and the text that joins
    it to the next credit ("feat.", " & ")."""

    name: str
    mbid: str | None = None
    artist_id: int | None = None
    join: str = ""


@dataclass
class TrackRow:
    disc: int
    number: str
    title: str
    seconds: int | None = None
    source: str = "none"  # file | preview | none
    url: str | None = None
    mime: str | None = None
    codec: str | None = None
    quality: str | None = None
    note: str | None = None  # not_playable | no_file | no_preview
    track_id: int | None = None  # the library track (owned rows): what "info" opens
    artists: list[ArtistRef] = field(default_factory=list)  # empty: the album's artist


def owned_album_for(db: Session, release_group_id: int) -> LibraryAlbum | None:
    return db.scalar(
        select(LibraryAlbum)
        .where(
            LibraryAlbum.release_group_id == release_group_id,
            LibraryAlbum.match_status.in_(OWNED_STATUSES),
        )
        .order_by(LibraryAlbum.id)
        .limit(1)
    )


def owned_rows(db: Session, album: LibraryAlbum) -> list[TrackRow]:
    tracks = db.scalars(
        select(LibraryTrack)
        .where(LibraryTrack.album_id == album.id)
        .order_by(LibraryTrack.disc_no, LibraryTrack.track_no, LibraryTrack.path)
    )
    rows = []
    for i, t in enumerate(tracks, 1):
        ext = extension_of(t.path)
        mime = PLAYABLE.get(ext)
        row = TrackRow(
            track_id=t.id,
            disc=t.disc_no or 1,
            number=str(t.track_no or i),
            title=t.title,
            seconds=round(t.duration) if t.duration else None,
            codec=t.codec or ext or None,
            quality=t.quality_tier,
            artists=[ArtistRef(t.artist.strip())] if (t.artist or "").strip() else [],
        )
        if t.path.startswith("plex:"):
            row.note = "no_file"
        elif mime is None:
            row.note = "not_playable"
        else:
            row.source, row.url, row.mime = "file", f"/stream/{t.id}", mime
        rows.append(row)
    return rows


def player_data(rows: list[TrackRow], *, album: str, artist: str, cover: str = "") -> dict:
    """What the album page hands to the player: the tracks that can be played from a file, each
    with the position of its row (the row's play button starts that track)."""
    queue = []
    for index, row in enumerate(rows, 1):
        if row.source != "file":
            continue
        credit = "".join(f"{a.name}{a.join}" for a in row.artists).strip()
        queue.append(
            {
                "row": index,
                "id": row.track_id,
                "title": row.title,
                "artist": credit or artist,
                "url": row.url,
                "seconds": row.seconds or 0,
                "number": row.number,
            }
        )
    return {"album": album, "artist": artist, "cover": cover, "queue": queue}


def _similar(a: str, b: str) -> float:
    return fuzz.ratio(name_key(a), name_key(b)) / 100


def match_previews(
    tracks: list[ReleaseTrack], previews: list[PreviewTrack]
) -> dict[int, PreviewTrack]:
    """{release track id: its preview}: by position first, then by similar title."""
    matched: dict[int, PreviewTrack] = {}
    used: set[int] = set()
    by_position = {(p.disc, p.position): p for p in previews}
    for t in tracks:
        p = by_position.get((t.disc, t.position))
        if p is not None and p.id not in used and _similar(t.title, p.title) >= POSITION_MATCH:
            matched[t.id] = p
            used.add(p.id)
    for t in tracks:
        if t.id in matched:
            continue
        best = max(
            (p for p in previews if p.id not in used),
            key=lambda p: _similar(t.title, p.title),
            default=None,
        )
        if best is not None and _similar(t.title, best.title) >= TITLE_MATCH:
            matched[t.id] = best
            used.add(best.id)
    return matched


def remote_rows(tracks: list[ReleaseTrack], previews: list[PreviewTrack]) -> list[TrackRow]:
    """Rows of an album that is not in the library."""
    if not tracks:  # no list from MusicBrainz: the preview tracks are all we know
        return [
            TrackRow(p.disc, str(p.position), p.title, source="preview", url=p.url)
            for p in sorted(previews, key=lambda p: (p.disc, p.position))
        ]
    matches = match_previews(tracks, previews)
    rows = []
    for t in tracks:
        row = TrackRow(
            disc=t.disc,
            number=t.number or str(t.position),
            title=t.title,
            seconds=round(t.length_ms / 1000) if t.length_ms else None,
            artists=_credits_of(t),
        )
        preview = matches.get(t.id)
        if preview is not None:
            row.source, row.url = "preview", preview.url
        else:
            row.note = "no_preview"
        rows.append(row)
    return rows


def _credits_of(track: ReleaseTrack) -> list[ArtistRef]:
    try:
        raw = json.loads(track.artist_credit_json or "[]")
    except ValueError:
        return []
    return [
        ArtistRef(str(c["name"]), c.get("mbid") or None, join=str(c.get("join") or ""))
        for c in raw
        if isinstance(c, dict) and c.get("name")
    ]


def attach_artists(db: Session, rows: list[TrackRow], fallback: Artist) -> None:
    """Give every row its artists, each linked to our artist page when we have that artist
    (by MusicBrainz id, else by name). A row without credits gets the album's artist."""
    for row in rows:
        if not row.artists:
            row.artists = [ArtistRef(fallback.name, fallback.mbid, fallback.id)]
    mbids = {a.mbid for r in rows for a in r.artists if a.mbid and a.artist_id is None}
    keys = {name_key(a.name) for r in rows for a in r.artists if a.artist_id is None}
    by_mbid: dict[str, int] = {}
    by_key: dict[str, int] = {}
    if mbids or keys:
        for artist in db.scalars(
            select(Artist).where(Artist.mbid.in_(mbids) | Artist.name_key.in_(keys))
        ):
            if artist.mbid:
                by_mbid[artist.mbid] = artist.id
            by_key.setdefault(artist.name_key, artist.id)
    for row in rows:
        for ref in row.artists:
            if ref.artist_id is None:
                ref.artist_id = by_mbid.get(ref.mbid or "") or by_key.get(name_key(ref.name))


def total_seconds(rows: list[TrackRow]) -> int:
    return sum(r.seconds or 0 for r in rows)
