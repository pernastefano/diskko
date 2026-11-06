"""Resolve a library artist to a MusicBrainz artist (MBID).

Order of evidence: the MBID in the file tags (handled by the library scan), the fixed MBID of
"Various Artists", then a name search. Namesakes (there are five "Nirvana") are told apart by
comparing the artist's library albums with each candidate's discography; whatever stays
unclear goes to the review queue instead of being guessed.
"""

from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from app.integrations.musicbrainz_client import ArtistHit, MusicBrainzClient
from app.models import MB_IGNORED, MB_RESOLVED, MB_REVIEW
from app.services.library.grouping import UNKNOWN_ARTIST, VARIOUS_ARTISTS, name_key
from app.services.musicbrainz.matching import title_similarity

# Fixed MBID of the "Various Artists" special purpose artist (official MusicBrainz docs).
VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"

SEARCH_LIMIT = 10
NAMESAKES_TO_COMPARE = 3  # how many same-name candidates get their discography compared
FUZZY_MIN_SIMILARITY = 92
FUZZY_MIN_SCORE = 90
FUZZY_MARGIN = 10
OVERLAP_TITLE_SIMILARITY = 90
REVIEW_CANDIDATES = 5


@dataclass
class Resolution:
    status: str  # resolved | review | ignored
    mbid: str | None = None
    source: str | None = None  # auto
    confidence: float | None = None
    reason: str | None = None  # ambiguous | low_score | no_match (when status == review)
    candidates: list[dict[str, Any]] = field(default_factory=list)


def _names(hit: ArtistHit) -> list[str]:
    return [hit.name, *hit.aliases]


def name_similarity(wanted: str, hit: ArtistHit) -> float:
    key = name_key(wanted)
    return max((fuzz.ratio(key, name_key(n)) for n in _names(hit)), default=0.0)


def _is_exact(wanted: str, hit: ArtistHit) -> bool:
    key = name_key(wanted)
    return any(name_key(n) == key for n in _names(hit))


def _candidate(hit: ArtistHit, wanted: str, overlap: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "mbid": hit.mbid,
        "name": hit.name,
        "sort_name": hit.sort_name,
        "disambiguation": hit.disambiguation,
        "country": hit.country,
        "type": hit.type,
        "score": hit.score,
        "similarity": round(name_similarity(wanted, hit)),
    }
    if overlap is not None:
        data["overlap"] = overlap
    return data


async def _discography_overlap(
    client: MusicBrainzClient, hit: ArtistHit, album_titles: list[str]
) -> int:
    """How many of the library albums appear in this artist's MusicBrainz discography."""
    groups = await client.browse_release_groups(hit.mbid) or []
    return sum(
        1
        for title in album_titles
        if any(title_similarity(title, g.title) >= OVERLAP_TITLE_SIMILARITY for g in groups)
    )


async def resolve_artist(
    client: MusicBrainzClient, name: str, album_titles: list[str]
) -> Resolution:
    if name_key(name) == name_key(VARIOUS_ARTISTS):
        return Resolution(MB_RESOLVED, VARIOUS_ARTISTS_MBID, "auto", 1.0)
    if name_key(name) == name_key(UNKNOWN_ARTIST):
        return Resolution(MB_IGNORED)

    hits = await client.search_artists(name, limit=SEARCH_LIMIT)
    if not hits:
        return Resolution(MB_REVIEW, reason="no_match")

    exact = [h for h in hits if _is_exact(name, h)]
    if len(exact) == 1:
        return Resolution(MB_RESOLVED, exact[0].mbid, "auto", min(exact[0].score, 100) / 100)

    if len(exact) > 1:  # namesakes: compare discographies
        contenders = sorted(exact, key=lambda h: -h.score)[:NAMESAKES_TO_COMPARE]
        overlaps = [await _discography_overlap(client, h, album_titles) for h in contenders]
        ranked = sorted(zip(overlaps, contenders, strict=True), key=lambda x: (-x[0], -x[1].score))
        best_overlap, best = ranked[0]
        second_overlap = ranked[1][0] if len(ranked) > 1 else 0
        if best_overlap >= 1 and (second_overlap == 0 or best_overlap - second_overlap >= 2):
            return Resolution(MB_RESOLVED, best.mbid, "auto", 0.9)
        shown = [_candidate(h, name, o) for o, h in ranked]
        shown += [_candidate(h, name) for h in exact[NAMESAKES_TO_COMPARE:REVIEW_CANDIDATES]]
        return Resolution(MB_REVIEW, reason="ambiguous", candidates=shown)

    # No exact name: only a very close, clearly best hit is accepted.
    scored = sorted(hits, key=lambda h: -name_similarity(name, h))
    top = scored[0]
    top_sim = name_similarity(name, top)
    next_sim = name_similarity(name, scored[1]) if len(scored) > 1 else 0.0
    if (
        top_sim >= FUZZY_MIN_SIMILARITY
        and top.score >= FUZZY_MIN_SCORE
        and top_sim - next_sim >= FUZZY_MARGIN
    ):
        return Resolution(MB_RESOLVED, top.mbid, "auto", 0.8)
    return Resolution(
        MB_REVIEW,
        reason="low_score",
        candidates=[_candidate(h, name) for h in scored[:REVIEW_CANDIDATES]],
    )
