"""Pictures of the library artists (an address at Deezer; the image is never copied)."""

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.integrations.discovery_types import ArtistCandidate
from app.models import Artist, utcnow
from app.services.library.grouping import name_key

RECHECK = timedelta(days=90)  # an artist without a picture is looked up again after this long


def pick_artist(candidates: list[ArtistCandidate], name: str) -> ArtistCandidate | None:
    """The artist with exactly this name (accents, case and punctuation aside) that has a
    picture; among namesakes the one with most fans. Anything looser risks a tribute band."""
    wanted = name_key(name)
    matches = [c for c in candidates if c.picture_url and name_key(c.name) == wanted]
    return max(matches, key=lambda c: c.fans) if matches else None


def artists_needing_image(db: Session, limit: int, *, now=utcnow) -> list[tuple[int, str]]:
    """(id, name) of library artists never looked up, or looked up long ago without success."""
    rows = db.execute(
        select(Artist.id, Artist.name)
        .where(
            Artist.in_library.is_(True),
            Artist.image_url.is_(None),
            or_(Artist.image_checked_at.is_(None), Artist.image_checked_at < now() - RECHECK),
        )
        .order_by(Artist.image_checked_at.is_not(None), Artist.id)
        .limit(limit)
    )
    return [(i, n) for i, n in rows]


def store_image(db: Session, artist_id: int, url: str | None) -> None:
    artist = db.get(Artist, artist_id)
    if artist is not None:
        artist.image_url = url
        artist.image_checked_at = utcnow()
        db.flush()
