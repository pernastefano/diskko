"""Missing albums: release groups of an artist that the library does not have.

Computed on the fly (never stored), so changing the selected types is instantly reflected.
"""

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    MATCH_MANUAL,
    MATCH_MATCHED,
    MATCH_TENTATIVE,
    LibraryAlbum,
    ReleaseGroup,
)
from app.services.app_settings import get_setting, set_setting
from app.services.musicbrainz.categories import CATEGORIES, DEFAULT_MISSING_TYPES

SETTING_KEY = "missing_types"
OWNED_STATUSES = (MATCH_MATCHED, MATCH_TENTATIVE, MATCH_MANUAL)


def _clean(types: Iterable[str]) -> list[str]:
    wanted = set(types)
    return [c for c in CATEGORIES if c in wanted]  # canonical order, unknown values dropped


def get_missing_types(db: Session, user_id: int | None = None) -> list[str]:
    """The user's own choice, else the global one, else the default."""
    if user_id is not None:
        own = get_setting(db, SETTING_KEY, None, user_id=user_id)
        if own is not None:
            return _clean(own)
    shared = get_setting(db, SETTING_KEY, None)
    return _clean(shared if shared is not None else DEFAULT_MISSING_TYPES)


def has_user_override(db: Session, user_id: int) -> bool:
    return get_setting(db, SETTING_KEY, None, user_id=user_id) is not None


def set_missing_types(db: Session, types: Iterable[str], user_id: int | None = None) -> None:
    """Save the global (user_id=None) or a personal list. The caller commits."""
    set_setting(db, SETTING_KEY, _clean(types), user_id=user_id)


def clear_user_missing_types(db: Session, user_id: int) -> None:
    """Go back to the global choice."""
    from sqlalchemy import delete

    from app.models import Setting

    db.execute(
        delete(Setting).where(
            Setting.key == SETTING_KEY, Setting.scope == "user", Setting.user_id == user_id
        )
    )


def owned_release_group_ids(db: Session, artist_id: int) -> set[int]:
    return {
        rg_id
        for (rg_id,) in db.execute(
            select(LibraryAlbum.release_group_id).where(
                LibraryAlbum.artist_id == artist_id,
                LibraryAlbum.release_group_id.is_not(None),
                LibraryAlbum.match_status.in_(OWNED_STATUSES),
            )
        )
    }


def missing_release_groups(db: Session, artist_id: int, types: Iterable[str]) -> list[ReleaseGroup]:
    wanted = _clean(types)
    if not wanted:
        return []
    owned = owned_release_group_ids(db, artist_id)
    stmt = (
        select(ReleaseGroup)
        .where(ReleaseGroup.artist_id == artist_id, ReleaseGroup.category.in_(wanted))
        .order_by(ReleaseGroup.year.is_(None), ReleaseGroup.year, ReleaseGroup.title)
    )
    return [rg for rg in db.scalars(stmt) if rg.id not in owned]


def missing_counts(db: Session, artist_ids: list[int], types: Iterable[str]) -> dict[int, int]:
    """{artist_id: number of missing release groups} for a page of artists (2 queries)."""
    wanted = _clean(types)
    counts = dict.fromkeys(artist_ids, 0)
    if not wanted or not artist_ids:
        return counts
    owned: dict[int, set[int]] = {}
    for artist_id, rg_id in db.execute(
        select(LibraryAlbum.artist_id, LibraryAlbum.release_group_id).where(
            LibraryAlbum.artist_id.in_(artist_ids),
            LibraryAlbum.release_group_id.is_not(None),
            LibraryAlbum.match_status.in_(OWNED_STATUSES),
        )
    ):
        owned.setdefault(artist_id, set()).add(rg_id)
    for artist_id, rg_id in db.execute(
        select(ReleaseGroup.artist_id, ReleaseGroup.id).where(
            ReleaseGroup.artist_id.in_(artist_ids), ReleaseGroup.category.in_(wanted)
        )
    ):
        if rg_id not in owned.get(artist_id, set()):
            counts[artist_id] += 1
    return counts


def release_group_count(db: Session, artist_id: int) -> int:
    return (
        db.scalar(select(func.count(ReleaseGroup.id)).where(ReleaseGroup.artist_id == artist_id))
        or 0
    )
