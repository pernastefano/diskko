"""Read models for the user-facing library pages (/library and /artist/<id>).

Everything here is built from a handful of SQL queries per page, whatever the size of the
library (no per-artist queries): the missing-album counts are aggregated in SQL with the same
rules as `missing_release_groups`, and a test keeps the two in step.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import exists, func, literal, select
from sqlalchemy.orm import Session

from app.models import Artist, LibraryAlbum, ReleaseGroup
from app.quality import LOSSLESS_CODECS
from app.services.library.grouping import name_key
from app.services.musicbrainz.categories import CATEGORIES
from app.services.musicbrainz.missing import OWNED_STATUSES

PAGE_SIZE = 48
SORTS = ("name", "missing", "recent")
UNMATCHED = "unmatched"  # pseudo-category of albums with no release group


# ---------------------------------------------------------------- artist listing


@dataclass
class ArtistRow:
    artist: Artist
    albums: int
    missing: int
    cover: str | None = None


def _owned_group_exists():
    """Correlated: some owned library album of the same artist is linked to this release group."""
    return exists().where(
        LibraryAlbum.release_group_id == ReleaseGroup.id,
        LibraryAlbum.artist_id == ReleaseGroup.artist_id,
        LibraryAlbum.match_status.in_(OWNED_STATUSES),
    )


def missing_counts_subquery(types: Iterable[str]):
    """Per-artist number of missing release groups of the wanted categories."""
    wanted = [c for c in CATEGORIES if c in set(types)]
    if not wanted:
        return None
    return (
        select(ReleaseGroup.artist_id.label("artist_id"), func.count().label("n"))
        .where(ReleaseGroup.category.in_(wanted), ~_owned_group_exists())
        .group_by(ReleaseGroup.artist_id)
        .subquery("missing")
    )


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_artists(
    db: Session,
    *,
    types: Iterable[str],
    q: str = "",
    letter: str = "",
    sort: str = "name",
    only_missing: bool = False,
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> tuple[list[ArtistRow], int]:
    """One page of library artists and the total number matching the filters."""
    albums = (
        select(LibraryAlbum.artist_id.label("artist_id"), func.count().label("n"))
        .group_by(LibraryAlbum.artist_id)
        .subquery("albums")
    )
    missing = missing_counts_subquery(types)
    albums_col = func.coalesce(albums.c.n, 0)
    missing_col = func.coalesce(missing.c.n, 0) if missing is not None else literal(0)

    base = select(Artist, albums_col.label("albums"), missing_col.label("missing")).outerjoin(
        albums, albums.c.artist_id == Artist.id
    )
    if missing is not None:
        base = base.outerjoin(missing, missing.c.artist_id == Artist.id)
    base = base.where(Artist.in_library.is_(True))

    needle = name_key(q) if q.strip() else ""
    if needle:
        base = base.where(Artist.name_key.like(f"%{_escape_like(needle)}%", escape="\\"))
    initial = letter.strip()[:1]
    if initial == "#":
        base = base.where(~func.substr(Artist.name_key, 1, 1).between("a", "z"))
    elif initial.isascii() and initial.isalpha():  # check before case-mapping: 'ß'.upper() == 'SS'
        base = base.where(func.substr(Artist.name_key, 1, 1) == initial.lower())
    if only_missing:
        base = base.where(missing_col > 0)

    total = db.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    if sort == "missing":
        order = (missing_col.desc(), func.lower(Artist.sort_name), Artist.id)
    elif sort == "recent":
        order = (Artist.created_at.desc(), Artist.id.desc())
    else:
        order = (func.lower(Artist.sort_name), Artist.id)
    page = max(page, 1)
    rows = db.execute(base.order_by(*order).limit(page_size).offset((page - 1) * page_size)).all()
    result = [ArtistRow(artist=r[0], albums=r[1], missing=r[2]) for r in rows]
    covers = representative_covers(db, [r.artist.id for r in result])
    for row in result:
        row.cover = covers.get(row.artist.id)
    return result, total


def available_letters(db: Session) -> set[str]:
    """Initial letters present in the library ('#' for anything that is not A-Z)."""
    letters = set()
    for (first,) in db.execute(
        select(func.substr(Artist.name_key, 1, 1)).where(Artist.in_library.is_(True)).distinct()
    ):
        letters.add(first.upper() if first and "a" <= first <= "z" else "#")
    return letters


def representative_covers(db: Session, artist_ids: list[int]) -> dict[int, str]:
    """For each artist, the cover of its earliest owned album that has one (one query)."""
    if not artist_ids:
        return {}
    rows = db.execute(
        select(LibraryAlbum.artist_id, ReleaseGroup.cover_path)
        .join(ReleaseGroup, ReleaseGroup.id == LibraryAlbum.release_group_id)
        .where(
            LibraryAlbum.artist_id.in_(artist_ids),
            LibraryAlbum.match_status.in_(OWNED_STATUSES),
            ReleaseGroup.cover_status == "ok",
            ReleaseGroup.cover_path.is_not(None),
        )
        .order_by(LibraryAlbum.artist_id, LibraryAlbum.year.is_(None), LibraryAlbum.year)
    )
    covers: dict[int, str] = {}
    for artist_id, path in rows:
        covers.setdefault(artist_id, path)
    return covers


# ------------------------------------------------------------------ artist page


@dataclass
class OwnedItem:
    album: LibraryAlbum
    group: ReleaseGroup | None


@dataclass
class ArtistSections:
    owned: list[tuple[str, list[OwnedItem]]] = field(default_factory=list)
    missing: list[tuple[str, list[ReleaseGroup]]] = field(default_factory=list)
    owned_total: int = 0
    missing_total: int = 0
    hidden_missing: int = 0  # missing release groups of types the user did not select
    covers_pending: bool = False  # some visible release group has no cover fetched yet


def _category_rank(category: str) -> int:
    return CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)


def build_artist_sections(
    db: Session, artist_id: int, types: Iterable[str], *, show_all: bool = False
) -> ArtistSections:
    wanted = set(CATEGORIES) if show_all else set(types)
    groups = list(db.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == artist_id)))
    by_id = {g.id: g for g in groups}
    albums = list(
        db.scalars(
            select(LibraryAlbum)
            .where(LibraryAlbum.artist_id == artist_id)
            .order_by(LibraryAlbum.year.is_(None), LibraryAlbum.year, LibraryAlbum.title)
        )
    )

    owned: dict[str, list[OwnedItem]] = defaultdict(list)
    owned_group_ids: set[int] = set()
    for album in albums:
        group = by_id.get(album.release_group_id) if album.match_status in OWNED_STATUSES else None
        if group is not None:
            owned_group_ids.add(group.id)
        owned[group.category if group else UNMATCHED].append(OwnedItem(album, group))

    missing: dict[str, list[ReleaseGroup]] = defaultdict(list)
    hidden = 0
    for group in sorted(groups, key=lambda g: (g.year is None, g.year or 0, g.title.casefold())):
        if group.id in owned_group_ids:
            continue
        if group.category in wanted:
            missing[group.category].append(group)
        else:
            hidden += 1

    shown_groups = [i.group for items in owned.values() for i in items if i.group] + [
        g for items in missing.values() for g in items
    ]
    return ArtistSections(
        owned=sorted(owned.items(), key=lambda kv: (kv[0] == UNMATCHED, _category_rank(kv[0]))),
        missing=sorted(missing.items(), key=lambda kv: _category_rank(kv[0])),
        owned_total=len(albums),
        missing_total=sum(len(v) for v in missing.values()),
        hidden_missing=hidden,
        covers_pending=any(g.cover_status == "unknown" for g in shown_groups),
    )


def describe_format(album: LibraryAlbum) -> str:
    """'FLAC', 'MP3 · 320 kbps' ... from the album's dominant codec and average bitrate."""
    codec = (album.format or "").upper()
    if not codec:
        return ""
    if (album.format or "").lower() in LOSSLESS_CODECS or not album.bitrate:
        return codec
    return f"{codec} · {album.bitrate} kbps"


# ------------------------------------------------------------ download requests


def request_statuses(db: Session, user_id: int, release_group_ids: list[int]) -> dict[int, str]:
    """{release_group_id: status} of the user's download requests.

    Download requests arrive with phases 7-8; until then nothing has been requested. The pages
    already render every status, so only this function needs to change.
    """
    return {}
