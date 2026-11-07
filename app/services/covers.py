"""Which artists still have covers to fetch (the nightly pass for artists nobody opens)."""

from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Artist, LibraryAlbum, ReleaseGroup
from app.services.musicbrainz.categories import CATEGORIES
from app.services.musicbrainz.missing import OWNED_STATUSES


def artists_needing_covers(db: Session, types: Iterable[str], limit: int) -> list[tuple[int, str]]:
    """(id, name) of library artists with a cover not looked up yet, for an album that matters:
    one of the wanted types (the ones shown as missing) or one that is owned.

    A cover the archive answered "none" for is marked missing, so it is not asked again; one that
    failed (a network error, a cancelled job) is still unknown and is asked for again.
    """
    wanted = [c for c in CATEGORIES if c in set(types)]
    owned = select(LibraryAlbum.release_group_id).where(
        LibraryAlbum.release_group_id.is_not(None), LibraryAlbum.match_status.in_(OWNED_STATUSES)
    )
    rows = db.execute(
        select(Artist.id, Artist.name)
        .join(ReleaseGroup, ReleaseGroup.artist_id == Artist.id)
        .where(
            Artist.in_library.is_(True),
            Artist.metadata_synced_at.is_not(None),
            ReleaseGroup.cover_status == "unknown",
            or_(ReleaseGroup.category.in_(wanted), ReleaseGroup.id.in_(owned)),
        )
        .group_by(Artist.id, Artist.name)
        .order_by(Artist.id)
        .limit(limit)
    )
    return [(i, n) for i, n in rows]
