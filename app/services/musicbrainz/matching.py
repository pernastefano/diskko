"""Match library albums to MusicBrainz release groups (no network: works on stored data)."""

from collections import Counter
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    MATCH_MANUAL,
    MATCH_MATCHED,
    MATCH_NONE,
    MATCH_TENTATIVE,
    MATCH_UNMATCHED,
    AlbumMatchOverride,
    LibraryAlbum,
    ReleaseGroup,
)
from app.services.musicbrainz.titles import album_key

AUTO_CONFIDENCE = 0.85  # at or above (with a near-identical title): matched
MIN_CONFIDENCE = 0.60  # at or above: tentative; below: unmatched
AUTO_TITLE_SIMILARITY = 90
MIN_TITLE_SIMILARITY = 70
AMBIGUITY_MARGIN = 0.02

W_TITLE, W_YEAR, W_TYPE = 0.80, 0.15, 0.05
_EPS = 1e-9


@dataclass
class Candidate:
    release_group_id: int
    confidence: float
    title_similarity: float


@dataclass
class MatchStats:
    matched: int = 0
    tentative: int = 0
    unmatched: int = 0
    manual: int = 0
    none: int = 0


def title_similarity(a: str, b: str) -> float:
    """0-100 similarity of two album titles (editions and punctuation ignored)."""
    ka, kb = album_key(a), album_key(b)
    if not ka or not kb:
        return 0.0
    return max(fuzz.ratio(ka, kb), fuzz.token_sort_ratio(ka, kb))


def year_score(album_year: int | None, rg_year: int | None) -> float:
    if album_year is None or rg_year is None:
        return 0.5  # unknown: neutral
    return {0: 1.0, 1: 0.8, 2: 0.5, 3: 0.3}.get(abs(album_year - rg_year), 0.0)


def size_class(track_count: int | None) -> str | None:
    """What a folder of this many tracks most likely is (None when unknown)."""
    if not track_count:
        return None
    return "single" if track_count <= 3 else "ep" if track_count <= 6 else "album"


_SIZE_ORDER = {"single": 0, "ep": 1, "album": 2}
_SIZE_AFFINITY = {0: 1.0, 1: 0.6, 2: 0.2}


def type_score(is_compilation: bool, rg_category: str, track_count: int | None = None) -> float:
    """How plausible the release group's type is for this album (compilation flag, size)."""
    if is_compilation:
        score = 1.0 if rg_category == "compilation" else 0.3
    else:
        score = 0.4 if rg_category == "compilation" else 1.0
    expected = size_class(track_count)
    if expected and rg_category in _SIZE_ORDER:
        score *= _SIZE_AFFINITY[abs(_SIZE_ORDER[expected] - _SIZE_ORDER[rg_category])]
    return score


def score_pair(
    album_title: str,
    album_year: int | None,
    is_compilation: bool,
    rg_title: str,
    rg_year: int | None,
    rg_category: str,
    track_count: int | None = None,
) -> tuple[float, float]:
    """(confidence 0-1, title similarity 0-100)."""
    sim = title_similarity(album_title, rg_title)
    confidence = (
        W_TITLE * sim / 100
        + W_YEAR * year_score(album_year, rg_year)
        + W_TYPE * type_score(is_compilation, rg_category, track_count)
    )
    return confidence, sim


def rank_candidates(album: LibraryAlbum, groups: list[ReleaseGroup]) -> list[Candidate]:
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
        if sim >= MIN_TITLE_SIMILARITY:
            ranked.append(Candidate(rg.id, confidence, sim))
    return sorted(ranked, key=lambda c: (-c.confidence, c.release_group_id))


def decide(candidates: list[Candidate]) -> tuple[Candidate | None, str]:
    """Best candidate and the resulting status."""
    if not candidates:
        return None, MATCH_UNMATCHED
    best = candidates[0]
    if best.confidence < MIN_CONFIDENCE - _EPS:
        return None, MATCH_UNMATCHED
    rivals = [
        c
        for c in candidates[1:]
        if best.confidence - c.confidence <= AMBIGUITY_MARGIN + _EPS
        and c.confidence >= MIN_CONFIDENCE
    ]
    confident = (
        best.confidence >= AUTO_CONFIDENCE - _EPS and best.title_similarity >= AUTO_TITLE_SIMILARITY
    )
    if confident and not rivals:
        return best, MATCH_MATCHED
    return best, MATCH_TENTATIVE


def match_artist_albums(db: Session, artist_id: int) -> MatchStats:
    """(Re)compute the match of every album of an artist. The caller commits."""
    groups = list(db.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == artist_id)))
    by_mbid = {g.mbid: g for g in groups}
    by_id = {g.id: g for g in groups}
    overrides = {
        o.album_key: o
        for o in db.scalars(
            select(AlbumMatchOverride).where(AlbumMatchOverride.artist_id == artist_id)
        )
    }
    stats = MatchStats()
    albums = db.scalars(select(LibraryAlbum).where(LibraryAlbum.artist_id == artist_id)).all()
    for album in albums:
        override = overrides.get(album_key(album.title))
        if override is not None and (
            override.release_group_id is None or override.release_group_id in by_id
        ):
            if override.release_group_id is None:
                album.release_group_id, album.matched_confidence = None, None
                album.match_status = MATCH_NONE
                stats.none += 1
            else:
                album.release_group_id, album.matched_confidence = override.release_group_id, 1.0
                album.match_status = MATCH_MANUAL
                stats.manual += 1
            continue
        tagged = by_mbid.get(album.mbid_release_group or "")
        if tagged is not None:
            album.release_group_id, album.matched_confidence = tagged.id, 1.0
            album.match_status = MATCH_MATCHED
            stats.matched += 1
            continue
        best, status = decide(rank_candidates(album, groups))
        album.release_group_id = (
            best.release_group_id if best and status != MATCH_UNMATCHED else None
        )
        album.matched_confidence = round(best.confidence, 3) if best else None
        album.match_status = status
        if status == MATCH_MATCHED:
            stats.matched += 1
        elif status == MATCH_TENTATIVE:
            stats.tentative += 1
        else:
            stats.unmatched += 1
    return stats


def candidates_for_review(
    db: Session, album: LibraryAlbum, limit: int = 8
) -> list[tuple[ReleaseGroup, float]]:
    """Release groups of the album's artist, best matches first (for the manual override UI)."""
    groups = list(db.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == album.artist_id)))
    scored = []
    for rg in groups:
        confidence, _ = score_pair(
            album.title,
            album.year,
            album.is_compilation,
            rg.title,
            rg.year,
            rg.category,
            album.track_count,
        )
        scored.append((rg, confidence))
    scored.sort(key=lambda item: (-item[1], item[0].title))
    return scored[:limit]


def status_counts(db: Session, artist_id: int) -> Counter:
    return Counter(
        db.scalars(select(LibraryAlbum.match_status).where(LibraryAlbum.artist_id == artist_id))
    )
