"""Previews: choosing the right album at a provider and keeping the track addresses."""

import re
from dataclasses import dataclass
from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.integrations.discovery_types import AlbumCandidate, PreviewData
from app.models import (
    PREVIEW_DEEZER,
    PREVIEW_FOUND,
    PREVIEW_NONE,
    PreviewLookup,
    PreviewTrack,
    utcnow,
)
from app.services.library.grouping import name_key

ARTIST_MIN = 0.85  # similarity of the artist names
TITLE_MIN = 0.88  # similarity of the album titles
# Deezer addresses are signed and last about 15 minutes: refresh a little earlier.
DEEZER_URL_LIFETIME = timedelta(minutes=10)
MISSING_RECHECK = timedelta(days=3)
FOUND_RECHECK = timedelta(days=30)

# "(Deluxe Edition)", "[Remastered 2009]"... do not make another album.
_EDITION = re.compile(
    r"[\(\[][^\)\]]*\b(deluxe|remaster\w*|edition|expanded|anniversary|bonus)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)


def title_core(title: str) -> str:
    return name_key(_EDITION.sub(" ", title))


def best_candidate(
    candidates: list[AlbumCandidate], artist: str, title: str, track_hint: int | None = None
) -> AlbumCandidate | None:
    """The candidate that is this artist's album, or None. Close titles ("OK Computer (8-bit)")
    and other artists never pass: a wrong preview is worse than none."""
    wanted_artist, wanted_title = name_key(artist), title_core(title)
    best: tuple[float, AlbumCandidate] | None = None
    for cand in candidates:
        artist_sim = fuzz.ratio(name_key(cand.artist), wanted_artist) / 100
        title_sim = fuzz.ratio(title_core(cand.title), wanted_title) / 100
        if artist_sim < ARTIST_MIN or title_sim < TITLE_MIN:
            continue
        score = title_sim + artist_sim / 10
        if track_hint and cand.track_count:
            score -= 0.02 * min(abs(cand.track_count - track_hint), 10)
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best else None


def store_previews(
    db: Session,
    release_group_id: int,
    provider: str | None,
    album_id: str | None,
    tracks: list[PreviewData],
    url: str | None = None,
) -> None:
    """Replace what is known about a release group. The caller commits."""
    db.execute(delete(PreviewTrack).where(PreviewTrack.release_group_id == release_group_id))
    now = utcnow()
    seen: set[tuple[int, int]] = set()
    for track in sorted(tracks, key=lambda t: (t.disc, t.position)):
        if (track.disc, track.position) in seen:
            continue
        seen.add((track.disc, track.position))
        db.add(
            PreviewTrack(
                release_group_id=release_group_id,
                disc=track.disc,
                position=track.position,
                title=track.title[:512],
                provider_track_id=track.provider_track_id,
                url=track.url,
                fetched_at=now,
            )
        )
    lookup = db.get(PreviewLookup, release_group_id) or PreviewLookup(
        release_group_id=release_group_id
    )
    lookup.provider = provider if tracks else None
    lookup.provider_album_id = album_id if tracks else None
    lookup.provider_url = url if tracks else None
    lookup.status = PREVIEW_FOUND if tracks else PREVIEW_NONE
    lookup.checked_at = now
    db.add(lookup)


@dataclass
class PreviewState:
    state: str  # ready | missing | unknown (never looked up) | stale (look up again)
    provider: str | None = None
    tracks: list[PreviewTrack] | None = None


def preview_state(db: Session, release_group_id: int, *, now=utcnow) -> PreviewState:
    lookup = db.get(PreviewLookup, release_group_id)
    if lookup is None:
        return PreviewState("unknown")
    age = now() - lookup.checked_at
    if lookup.status == PREVIEW_NONE:
        return PreviewState("missing" if age < MISSING_RECHECK else "stale")
    tracks = list(
        db.scalars(
            select(PreviewTrack)
            .where(PreviewTrack.release_group_id == release_group_id)
            .order_by(PreviewTrack.disc, PreviewTrack.position)
        )
    )
    lifetime = DEEZER_URL_LIFETIME if lookup.provider == PREVIEW_DEEZER else FOUND_RECHECK
    if not tracks or age >= lifetime:
        return PreviewState("stale", lookup.provider)
    return PreviewState("ready", lookup.provider, tracks)
