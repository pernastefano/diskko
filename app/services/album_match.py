"""Matching one library album with MusicBrainz by hand: the candidates, the search, the decision."""

import json
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.integrations.musicbrainz_client import ReleaseGroupHit
from app.models import AlbumMatchOverride, LibraryAlbum, ReleaseGroup, User, utcnow
from app.services import audit
from app.services.musicbrainz.matching import match_artist_albums, score_pair
from app.services.musicbrainz.titles import album_key

SEARCH_LIMIT = 15
SOURCE_MUSICBRAINZ = "musicbrainz"


class MatchError(Exception):
    """A decision that cannot be applied; `code` is an i18n error key suffix."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class LocalCandidate:
    group: ReleaseGroup
    confidence: float  # 0-1
    similarity: float  # title similarity 0-100


def local_candidates(db: Session, album: LibraryAlbum) -> list[LocalCandidate]:
    """Every release group stored for the album's artist, the likeliest first (no cut-off: the
    person choosing sees what the automatic match would not offer)."""
    groups = db.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == album.artist_id))
    ranked = []
    for rg in groups:
        confidence, sim = score_pair(
            album.title,
            album.year,
            album.is_compilation,
            rg.title,
            rg.year,
            rg.category,
            album.track_count,
        )
        ranked.append(LocalCandidate(rg, confidence, sim))
    return sorted(ranked, key=lambda c: (-c.confidence, c.group.id))


def adopt_hit(db: Session, artist_id: int, hit: ReleaseGroupHit) -> ReleaseGroup:
    """The release group of a search result as one of the artist's (a compilation or another
    artist's album is not in the artist's discography, but it is the album's match). The caller
    commits. An existing row is reused; a new one is kept by later syncs while it is in use."""
    data = hit.data
    rg = db.scalar(
        select(ReleaseGroup).where(
            ReleaseGroup.artist_id == artist_id, ReleaseGroup.mbid == data.mbid
        )
    )
    if rg is None:
        rg = ReleaseGroup(mbid=data.mbid, artist_id=artist_id)
        db.add(rg)
    rg.title, rg.primary_type = data.title, data.primary_type
    rg.secondary_types_json = json.dumps(data.secondary_types)
    rg.first_release_date, rg.year = data.first_release_date, data.year
    rg.disambiguation, rg.category = data.disambiguation, data.category
    rg.source = SOURCE_MUSICBRAINZ
    rg.synced_at = utcnow()
    db.flush()
    return rg


def apply_choice(
    db: Session, album: LibraryAlbum, user: User, choice: str, *, artist_name: str = ""
) -> None:
    """Record a manual decision for an album: a release group id of its artist, 'none' (not on
    MusicBrainz) or 'auto' (undo, back to the automatic match). The caller commits."""
    key = album_key(album.title)
    existing = db.scalar(
        select(AlbumMatchOverride).where(
            and_(
                AlbumMatchOverride.artist_id == album.artist_id, AlbumMatchOverride.album_key == key
            )
        )
    )
    if choice == "auto":
        if existing is not None:
            db.delete(existing)
    else:
        group_id = None
        if choice != "none":
            group = db.get(ReleaseGroup, int(choice)) if choice.isdigit() else None
            if group is None or group.artist_id != album.artist_id:
                raise MatchError("rg_invalid")
            group_id = group.id
        if existing is None:
            existing = AlbumMatchOverride(artist_id=album.artist_id, album_key=key)
            db.add(existing)
        existing.release_group_id, existing.created_by = group_id, user.id
    db.flush()
    match_artist_albums(db, album.artist_id)
    audit.record(
        db,
        user,
        "album.match",
        target_type="album",
        target_id=album.id,
        label=f"{artist_name} – {album.title}".strip(" –"),
        details={"choice": choice},
    )
