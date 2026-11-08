"""Database side of the MusicBrainz sync (the job calls these through a short session)."""

import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.integrations.musicbrainz_client import ReleaseGroupData
from app.models import (
    MATCH_UNMATCHED,
    MB_IGNORED,
    MB_PENDING,
    MB_PROVIDER,
    MB_RESOLVED,
    MB_REVIEW,
    MB_UNMATCHED,
    REVIEW_DISMISSED,
    REVIEW_PENDING,
    REVIEW_RESOLVED,
    SOURCE_MUSICBRAINZ,
    AlbumMatchOverride,
    Artist,
    ArtistReview,
    DownloadRequest,
    LibraryAlbum,
    ReleaseGroup,
    utcnow,
)
from app.services.musicbrainz.resolution import Resolution


@dataclass
class ArtistInput:
    id: int
    name: str
    mbid: str | None
    mb_status: str
    in_library: bool
    album_titles: list[str]
    provider_artist_id: str | None = None


def load_artist_input(db: Session, artist_id: int) -> ArtistInput | None:
    artist = db.get(Artist, artist_id)
    if artist is None:
        return None
    titles = list(db.scalars(select(LibraryAlbum.title).where(LibraryAlbum.artist_id == artist_id)))
    return ArtistInput(
        artist.id,
        artist.name,
        artist.mbid,
        artist.mb_status,
        artist.in_library,
        titles,
        artist.provider_artist_id,
    )


def _close_reviews(db: Session, artist_id: int, status: str, user_id: int | None = None) -> None:
    for review in db.scalars(
        select(ArtistReview).where(
            ArtistReview.artist_id == artist_id, ArtistReview.status == REVIEW_PENDING
        )
    ):
        review.status, review.resolved_at, review.resolved_by = status, utcnow(), user_id


def open_review(db: Session, artist_id: int, reason: str, candidates: list[dict]) -> None:
    """Put an artist in the review queue (one open item per artist)."""
    _close_reviews(db, artist_id, REVIEW_DISMISSED)
    db.add(
        ArtistReview(
            artist_id=artist_id,
            reason=reason,
            candidates_json=json.dumps(candidates),
            status=REVIEW_PENDING,
        )
    )
    artist = db.get(Artist, artist_id)
    artist.mb_status = MB_REVIEW


def apply_resolution(db: Session, artist_id: int, resolution: Resolution) -> None:
    artist = db.get(Artist, artist_id)
    if resolution.status == MB_RESOLVED:
        _set_mbid(db, artist, resolution.mbid, resolution.source or "auto", resolution.confidence)
    elif resolution.status == MB_IGNORED:
        artist.mb_status = MB_IGNORED
        _close_reviews(db, artist_id, REVIEW_DISMISSED)
    else:
        open_review(db, artist_id, resolution.reason or "no_match", resolution.candidates)


def _set_mbid(
    db: Session, artist: Artist, mbid: str, source: str, confidence: float | None
) -> None:
    other = db.scalar(select(Artist).where(Artist.mbid == mbid, Artist.id != artist.id))
    if other is not None:
        # Two library artists with the same MBID would break the unique constraint: the
        # name differs only by spelling. Keep the first and send this one to review.
        open_review(
            db,
            artist.id,
            "ambiguous",
            [{"mbid": mbid, "name": other.name, "note": "already assigned to another artist"}],
        )
        return
    clear_provider_data(db, artist)  # MusicBrainz is the reference: Deezer's data goes
    artist.mbid, artist.mb_status = mbid, MB_RESOLVED
    artist.mb_source, artist.mb_confidence = source, confidence
    _close_reviews(db, artist.id, REVIEW_RESOLVED)


def mark_invalid_mbid(db: Session, artist_id: int) -> None:
    open_review(db, artist_id, "invalid_mbid", [])


def store_release_groups(
    db: Session,
    artist_id: int,
    groups: list[ReleaseGroupData],
    *,
    source: str = SOURCE_MUSICBRAINZ,
    prune: bool = True,
) -> tuple[int, int, int]:
    """Upsert the discography of one source; release groups of that source that vanished from
    it are removed (unless `prune` is off). Groups of other sources are never touched."""
    existing = {
        rg.mbid: rg
        for rg in db.scalars(
            select(ReleaseGroup).where(
                ReleaseGroup.artist_id == artist_id, ReleaseGroup.source == source
            )
        )
    }
    added = updated = 0
    seen: set[str] = set()
    for data in groups:
        if data.mbid in seen:
            continue
        seen.add(data.mbid)
        rg = existing.get(data.mbid)
        if rg is None:
            rg = ReleaseGroup(mbid=data.mbid, artist_id=artist_id)
            db.add(rg)
            added += 1
        else:
            updated += 1
        rg.title, rg.primary_type = data.title, data.primary_type
        rg.secondary_types_json = json.dumps(data.secondary_types)
        rg.first_release_date, rg.year = data.first_release_date, data.year
        rg.disambiguation, rg.category = data.disambiguation, data.category
        rg.source = source
        rg.synced_at = utcnow()
    # A release group someone chose by hand for an album (an override points at it) stays even
    # when the artist's discography does not list it (a compilation, another artist's album).
    # So does one that has a download request: deleting it would delete the request, its files
    # and its notifications while a job is still working on it.
    chosen = set(
        db.scalars(
            select(AlbumMatchOverride.release_group_id).where(
                AlbumMatchOverride.release_group_id.is_not(None)
            )
        )
    ) | set(db.scalars(select(DownloadRequest.release_group_id)))
    removed = 0
    for mbid, rg in existing.items():
        if prune and mbid not in seen and rg.id not in chosen:
            db.delete(rg)
            removed += 1
    db.flush()
    return added, updated, removed


def unmatched_titles(db: Session, artist_id: int) -> list[str]:
    """Titles of the library albums that matched no release group."""
    return list(
        db.scalars(
            select(LibraryAlbum.title).where(
                LibraryAlbum.artist_id == artist_id, LibraryAlbum.match_status == MATCH_UNMATCHED
            )
        )
    )


# ------------------------------------------------- artists known only to a fallback provider


def clear_provider_data(db: Session, artist: Artist) -> None:
    """Forget what a fallback provider told about the artist (its albums included)."""
    ids = list(
        db.scalars(
            select(ReleaseGroup.id).where(
                ReleaseGroup.artist_id == artist.id, ReleaseGroup.source != SOURCE_MUSICBRAINZ
            )
        )
    )
    if artist.provider is None and not ids:
        return
    if artist.provider is not None and ids:
        for album in db.scalars(select(LibraryAlbum).where(LibraryAlbum.release_group_id.in_(ids))):
            album.release_group_id, album.matched_confidence = None, None
            album.match_status = MATCH_UNMATCHED
        for rg in db.scalars(select(ReleaseGroup).where(ReleaseGroup.id.in_(ids))):
            db.delete(rg)
    artist.provider, artist.provider_artist_id = None, None
    db.flush()


def apply_provider(
    db: Session,
    artist_id: int,
    provider: str,
    provider_artist_id: str,
    confidence: float,
    details: dict,
) -> None:
    """The artist is not on MusicBrainz but a provider knows it for sure. The first time, it is
    queued for a human look (the data can be confirmed, replaced or dropped later)."""
    artist = db.get(Artist, artist_id)
    first_time = artist.provider is None
    artist.provider, artist.provider_artist_id = provider, provider_artist_id
    artist.mb_status, artist.mb_source, artist.mb_confidence = MB_PROVIDER, "provider", confidence
    if first_time:
        _close_reviews(db, artist_id, REVIEW_DISMISSED)
        db.add(
            ArtistReview(
                artist_id=artist_id,
                reason="provider",
                candidates_json=json.dumps([{"provider": provider, **details}]),
                status=REVIEW_PENDING,
            )
        )


def finish_provider_sync(db: Session, artist_id: int) -> None:
    artist = db.get(Artist, artist_id)
    artist.metadata_synced_at = utcnow()
    artist.mb_status = MB_PROVIDER


def confirm_provider(db: Session, artist_id: int, user_id: int | None) -> None:
    """An admin checked the fallback data and keeps it (MusicBrainz still wins if it appears)."""
    artist = db.get(Artist, artist_id)
    if artist.mb_status == MB_PROVIDER:
        artist.mb_source = "manual"
    _close_reviews(db, artist_id, REVIEW_RESOLVED, user_id)


def finish_sync(db: Session, artist_id: int) -> None:
    artist = db.get(Artist, artist_id)
    artist.metadata_synced_at = utcnow()
    artist.mb_status = MB_RESOLVED


# ------------------------------------------------------------- admin decisions


def choose_candidate(db: Session, artist_id: int, mbid: str, user_id: int | None) -> None:
    """An admin picked (or typed) the right MBID. The next sync verifies it exists."""
    artist = db.get(Artist, artist_id)
    _set_mbid(db, artist, mbid, "manual", 1.0)
    _close_reviews(db, artist_id, REVIEW_RESOLVED, user_id)
    artist.metadata_synced_at = None  # force a fresh sync


def dismiss_artist(db: Session, artist_id: int, user_id: int | None) -> None:
    """An admin decided the artist is not on MusicBrainz: stop looking."""
    artist = db.get(Artist, artist_id)
    clear_provider_data(db, artist)
    artist.mb_status = MB_UNMATCHED
    _close_reviews(db, artist_id, REVIEW_DISMISSED, user_id)


def reset_artist(db: Session, artist_id: int, user_id: int | None) -> None:
    """Forget the current identity and search again."""
    artist = db.get(Artist, artist_id)
    clear_provider_data(db, artist)
    artist.mbid, artist.mb_source, artist.mb_confidence = None, None, None
    artist.mb_status, artist.metadata_synced_at = MB_PENDING, None
    _close_reviews(db, artist_id, REVIEW_DISMISSED, user_id)


def pending_review_candidates(review: ArtistReview) -> list[dict]:
    return json.loads(review.candidates_json or "[]")


def artists_needing_sync(db: Session, refresh_days: int) -> list[int]:
    """Library artists never looked up, or whose metadata is older than `refresh_days`."""
    cutoff = utcnow() - timedelta(days=refresh_days)
    stmt = select(Artist.id).where(
        Artist.in_library.is_(True),
        or_(
            Artist.mb_status == MB_PENDING,
            Artist.mb_status.in_((MB_RESOLVED, MB_PROVIDER))
            & (or_(Artist.metadata_synced_at.is_(None), Artist.metadata_synced_at < cutoff)),
        ),
    )
    return list(db.scalars(stmt.order_by(Artist.id)))
