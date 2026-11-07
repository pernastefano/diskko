"""Track lists of albums that are not (or not only) in the library: from MusicBrainz."""

import json
from collections import Counter
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.integrations.musicbrainz_client import ReleaseData
from app.models import ReleaseGroup, ReleaseTrack, TracklistFetch, utcnow

REFRESH = timedelta(days=30)


def choose_release(releases: list[ReleaseData]) -> ReleaseData | None:
    """The most representative release: the usual number of tracks, then the earliest.

    Reissues add bonus tracks, so the track count most releases agree on is the album itself.
    """
    usable = [r for r in releases if r.tracks]
    if not usable:
        return None
    counts = Counter(len(r.tracks) for r in usable)
    best = max(counts.values())
    wanted = min(n for n, c in counts.items() if c == best)  # a tie: the shorter one
    candidates = [r for r in usable if len(r.tracks) == wanted]
    return min(candidates, key=lambda r: (r.date or "9999", r.mbid))


def store_tracklist(db: Session, release_group_id: int, release: ReleaseData | None) -> int:
    """Replace the stored tracks (the caller commits). Also fills the track count hint."""
    db.execute(delete(ReleaseTrack).where(ReleaseTrack.release_group_id == release_group_id))
    seen: set[tuple[int, int]] = set()
    count = 0
    for t in release.tracks if release else []:
        if (t.disc, t.position) in seen:
            continue
        seen.add((t.disc, t.position))
        db.add(
            ReleaseTrack(
                release_group_id=release_group_id,
                disc=t.disc,
                position=t.position,
                number=t.number,
                title=t.title[:512],
                length_ms=t.length_ms,
                recording_mbid=t.recording_mbid,
                artist_credit_json=(
                    json.dumps([{"name": n, "mbid": m, "join": j} for n, m, j in t.credits])
                    if t.credits
                    else None
                ),
                release_mbid=release.mbid if release else None,
            )
        )
        count += 1
    fetch = db.get(TracklistFetch, release_group_id) or TracklistFetch(
        release_group_id=release_group_id
    )
    fetch.fetched_at = utcnow()
    fetch.release_mbid = release.mbid if release else None
    fetch.track_count = count
    db.add(fetch)
    group = db.get(ReleaseGroup, release_group_id)
    if group is not None and count:
        group.track_count_hint = count
    db.flush()
    return count


def tracklist_state(db: Session, release_group_id: int, *, now=utcnow) -> str:
    """'unknown' (never looked up), 'fresh' or 'stale'."""
    fetch = db.get(TracklistFetch, release_group_id)
    if fetch is None:
        return "unknown"
    return "fresh" if fetch.fetched_at + REFRESH > now() else "stale"


def load_tracks(db: Session, release_group_id: int) -> list[ReleaseTrack]:
    return list(
        db.scalars(
            select(ReleaseTrack)
            .where(ReleaseTrack.release_group_id == release_group_id)
            .order_by(ReleaseTrack.disc, ReleaseTrack.position)
        )
    )
