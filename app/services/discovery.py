"""Discovery: opening artists from outside the library and the similar artists lists."""

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.integrations.discovery_types import SimilarHit
from app.models import (
    MB_RESOLVED,
    Artist,
    LibraryAlbum,
    SimilarArtist,
    SimilarFetch,
    utcnow,
)
from app.services.library.grouping import name_key, sort_name_for

SIMILAR_REFRESH = timedelta(days=14)
SIMILAR_MAX = 30
EXTERNAL_ARTIST_DAYS = 90  # artists opened from a search are forgotten after this long
SOURCE_SEARCH = "search"  # Artist.mb_source of an artist opened from a search or a similar list
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def valid_mbid(value: str | None) -> bool:
    return bool(value) and bool(_UUID.match(str(value).lower()))


def clean_query(raw: str) -> str:
    """A search text: blanks collapsed, no control characters, bounded length."""
    text = " ".join("".join(c for c in raw if c.isprintable() or c.isspace()).split())
    return text[:100]


# ------------------------------------------------------------ opening an artist


def open_external_artist(
    db: Session,
    mbid: str,
    name: str,
    *,
    country: str | None = None,
    disambiguation: str | None = None,
) -> tuple[Artist, bool]:
    """The artist with this MBID, created (out of the library) if it is new. The caller commits.

    Returns (artist, created). An artist that is already known, in the library or not, is reused.
    """
    mbid = mbid.lower()
    artist = db.scalar(select(Artist).where(Artist.mbid == mbid))
    if artist is not None:
        return artist, False
    artist = Artist(
        name=name,
        sort_name=sort_name_for(name),
        name_key=name_key(name),
        mbid=mbid,
        mb_status=MB_RESOLVED,
        mb_source=SOURCE_SEARCH,
        country=country,
        disambiguation=disambiguation or None,
        in_library=False,
    )
    db.add(artist)
    db.flush()
    return artist, True


# --------------------------------------------------------------- similar artists


def similar_is_fresh(db: Session, artist_id: int, *, now=utcnow) -> bool:
    fetch = db.get(SimilarFetch, artist_id)
    return fetch is not None and fetch.fetched_at + SIMILAR_REFRESH > now()


def store_similar(db: Session, artist_id: int, hits: list[SimilarHit], source: str | None) -> int:
    """Replace the stored list. Hits for the artist itself and repeated ones are dropped."""
    artist = db.get(Artist, artist_id)
    own = {artist.name_key} if artist else set()
    db.execute(delete(SimilarArtist).where(SimilarArtist.artist_id == artist_id))
    seen: set[str] = set()
    kept = 0
    for hit in sorted(hits, key=lambda h: -h.score):
        key = hit.mbid or name_key(hit.name)
        if key in seen or name_key(hit.name) in own or (artist and hit.mbid == artist.mbid):
            continue
        seen.add(key)
        db.add(
            SimilarArtist(
                artist_id=artist_id,
                name=hit.name[:255],
                name_key=name_key(hit.name)[:255],
                mbid=hit.mbid,
                score=hit.score,
                source=source or "",
                position=kept,
            )
        )
        kept += 1
        if kept >= SIMILAR_MAX:
            break
    fetch = db.get(SimilarFetch, artist_id) or SimilarFetch(artist_id=artist_id)
    fetch.fetched_at, fetch.source, fetch.count = utcnow(), source, kept
    db.add(fetch)
    return kept


@dataclass
class SimilarItem:
    name: str
    mbid: str | None
    score: float
    source: str
    artist_id: int | None  # the artist row, if we know one (opened or in the library)
    in_library: bool


def list_similar(db: Session, artist_id: int) -> list[SimilarItem]:
    """The stored list with the "in library" flag, computed now so scans keep it right."""
    rows = list(
        db.scalars(
            select(SimilarArtist)
            .where(SimilarArtist.artist_id == artist_id)
            .order_by(SimilarArtist.position)
        )
    )
    if not rows:
        return []
    mbids = [r.mbid for r in rows if r.mbid]
    keys = [r.name_key for r in rows]
    known = db.scalars(
        select(Artist).where(
            or_(Artist.mbid.in_(mbids) if mbids else False, Artist.name_key.in_(keys))
        )
    ).all()
    by_mbid = {a.mbid: a for a in known if a.mbid}
    by_key: dict[str, Artist] = {}
    for a in known:  # a library artist wins over an artist that was only opened
        if a.name_key not in by_key or (a.in_library and not by_key[a.name_key].in_library):
            by_key[a.name_key] = a
    items = []
    for r in rows:
        match = by_mbid.get(r.mbid) if r.mbid else None
        match = match or by_key.get(r.name_key)
        items.append(
            SimilarItem(
                r.name,
                r.mbid,
                r.score,
                r.source,
                match.id if match else None,
                bool(match and match.in_library),
            )
        )
    return items


def purge_external_artists(db: Session, days: int) -> int:
    """Delete artists that were only opened from a search or a similar list (they are out of
    the library, own no album and are older than `days`). Their lists go with them."""
    cutoff = utcnow() - timedelta(days=days)
    owning = select(LibraryAlbum.artist_id)
    ids = db.scalars(
        select(Artist.id).where(
            Artist.in_library.is_(False),
            Artist.mb_source == SOURCE_SEARCH,
            Artist.created_at < cutoff,
            Artist.id.not_in(owning),
        )
    ).all()
    for artist_id in ids:
        db.delete(db.get(Artist, artist_id))
    db.commit()
    return len(ids)
