"""Giving an album to the right artist by hand.

A scan names the artist of an album by what the tags or Plex say, so an artist can be confused
with another that has the same or a similar name. The decision is kept per album (its folder and
title) and applied by every later scan, whatever the tags say.
"""

from dataclasses import dataclass

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from app.integrations.musicbrainz_client import ArtistHit
from app.models import (
    AlbumArtistOverride,
    AlbumMatchOverride,
    Artist,
    LibraryAlbum,
    User,
)
from app.models.musicbrainz import MATCH_UNMATCHED
from app.services import audit
from app.services.library.grouping import name_key, sort_name_for
from app.services.musicbrainz.matching import match_artist_albums
from app.services.musicbrainz.titles import album_key

SEARCH_LIMIT = 15


class ArtistChoiceError(Exception):
    """A decision that cannot be applied; `code` is an i18n error key suffix."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class ArtistChoice:
    artist: Artist
    albums: int  # library albums it has now


def find_artists(db: Session, text: str, *, exclude_id: int | None = None) -> list[ArtistChoice]:
    """The artists whose name contains the text (case, accents and spacing do not matter)."""
    key = name_key(text)
    if not key:
        return []
    like = "%" + key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    stmt = (
        select(Artist, func.count(LibraryAlbum.id))
        .outerjoin(LibraryAlbum, LibraryAlbum.artist_id == Artist.id)
        .where(Artist.name_key.like(like, escape="\\"))
        .group_by(Artist.id)
        .order_by(Artist.name_key, Artist.id)
        .limit(SEARCH_LIMIT)
    )
    if exclude_id is not None:
        stmt = stmt.where(Artist.id != exclude_id)
    return [ArtistChoice(artist=a, albums=n) for a, n in db.execute(stmt)]


def reassign_to_hit(db: Session, album: LibraryAlbum, user: User, hit: ArtistHit) -> Artist:
    """Move an album to the artist of a MusicBrainz search result: the artist that already has
    that MBID, or a new one made from the result. The caller commits."""
    from app.services.discovery import open_external_artist

    target, _ = open_external_artist(
        db, hit.mbid, hit.name, country=hit.country, disambiguation=hit.disambiguation
    )
    return reassign(db, album, user, artist_id=target.id)


def reassign(
    db: Session,
    album: LibraryAlbum,
    user: User,
    *,
    artist_id: int | None = None,
    new_name: str = "",
) -> Artist:
    """Move an album to another artist (one that exists, or a new one by name: also when another
    artist has that name, because telling namesakes apart is the point). The decision is kept
    for the later scans and both artists are matched again. The caller commits."""
    old = db.get(Artist, album.artist_id)
    if artist_id is not None:
        target = db.get(Artist, artist_id)
        if target is None:
            raise ArtistChoiceError("artist_invalid")
    else:
        name = " ".join(new_name.split())[:255]
        if not name:
            raise ArtistChoiceError("artist_name_empty")
        target = Artist(
            name=name, sort_name=sort_name_for(name), name_key=name_key(name), in_library=True
        )
        db.add(target)
        db.flush()
    if target.id == album.artist_id:
        raise ArtistChoiceError("artist_same")

    existing = db.scalar(
        select(AlbumArtistOverride).where(
            AlbumArtistOverride.path == album.path, AlbumArtistOverride.title == album.title
        )
    )
    if existing is None:
        existing = AlbumArtistOverride(path=album.path, title=album.title)
        db.add(existing)
    existing.artist_id, existing.created_by = target.id, user.id
    # what was decided about the match belongs to the old artist's discography
    db.execute(
        delete(AlbumMatchOverride).where(
            and_(
                AlbumMatchOverride.artist_id == album.artist_id,
                AlbumMatchOverride.album_key == album_key(album.title),
            )
        )
    )
    album.artist_id = target.id
    album.release_group_id, album.matched_confidence = None, None
    album.match_status = MATCH_UNMATCHED
    target.in_library = True
    db.flush()
    for artist in (old, target):
        if artist is not None and artist.metadata_synced_at is not None:
            match_artist_albums(db, artist.id)
    if old is not None and not db.scalar(
        select(LibraryAlbum.id).where(LibraryAlbum.artist_id == old.id).limit(1)
    ):
        old.in_library = False  # nothing left of it here; its data and decisions stay
    audit.record(
        db,
        user,
        "album.artist",
        target_type="album",
        target_id=album.id,
        label=f"{target.name} – {album.title}",
        details={"from": old.name if old else "", "to": target.name},
    )
    return target
