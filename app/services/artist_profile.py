"""The artist page's profile: picture, short biography and top tracks (data, no network)."""

import re
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.integrations.discovery_types import ArtistCandidate, BioData, TopTrackData
from app.models import (
    TOP_DEEZER,
    Artist,
    ArtistBio,
    ArtistTopTrack,
    utcnow,
)
from app.services.library.grouping import name_key

TOP_LIMIT = 10
BIO_LANGS = ("en", "it")  # the languages the interface speaks
BIO_RECHECK = timedelta(days=30)
TOP_RECHECK = timedelta(days=7)
DEEZER_PREVIEW_LIFETIME = timedelta(minutes=10)  # their addresses are signed and expire
BIO_MAX_CHARS = 300
BIO_MAX_SENTENCES = 2

# a sentence ends after two lowercase letters or digits ("A. B. Smith" and "Dr. Dre" do not)
_SENTENCE_END = re.compile(r"(?<=[a-zà-ÿ0-9]{2}[.!?])\s+(?=[\"“«(A-ZÀ-ÖØ-Þ0-9])")


def short_bio(text: str, max_chars: int = BIO_MAX_CHARS, sentences: int = BIO_MAX_SENTENCES) -> str:
    """The first sentences of a biography, at most `sentences` and about `max_chars` long, cut
    on a sentence end. One long first sentence is kept whole up to twice the limit, then cut at
    a word with an ellipsis."""
    text = " ".join(text.split())
    if not text:
        return ""
    parts = _SENTENCE_END.split(text)
    chosen = parts[0]
    for sentence in parts[1:sentences]:
        candidate = f"{chosen} {sentence}"
        if len(candidate) > max_chars:
            break
        chosen = candidate
    if len(chosen) > max_chars * 2:
        chosen = chosen[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return chosen


def pick_deezer_artist(candidates: list[ArtistCandidate], name: str) -> ArtistCandidate | None:
    """The Deezer artist with exactly this name (accents, case and punctuation aside), the one
    with most fans among namesakes. Anything looser risks a tribute band."""
    wanted = name_key(name)
    matches = [c for c in candidates if c.provider_id and name_key(c.name) == wanted]
    return max(matches, key=lambda c: c.fans) if matches else None


def deezer_id_of(artist: Artist) -> str | None:
    if artist.deezer_artist_id:
        return artist.deezer_artist_id
    return artist.provider_artist_id if artist.provider == "deezer" else None


# -------------------------------------------------------------------- biography


def get_bio(db: Session, artist_id: int, lang: str) -> ArtistBio | None:
    """The biography in this language, else in English; None when there is none."""
    rows = {
        b.lang: b for b in db.scalars(select(ArtistBio).where(ArtistBio.artist_id == artist_id))
    }
    for wanted in (lang, "en"):
        row = rows.get(wanted)
        if row is not None and row.text:
            return row
    return None


def bio_is_fresh(artist: Artist, *, now: datetime | None = None) -> bool:
    checked = artist.profile_checked_at
    return checked is not None and (now or utcnow()) - checked < BIO_RECHECK


def store_bio(db: Session, artist_id: int, lang: str, bio: BioData | None, source: str | None):
    """Keep what was found for a language (or that nothing was). The caller commits."""
    row = db.scalar(
        select(ArtistBio).where(ArtistBio.artist_id == artist_id, ArtistBio.lang == lang)
    )
    if row is None:
        row = ArtistBio(artist_id=artist_id, lang=lang)
        db.add(row)
    row.text = bio.text[:4000] if bio else ""
    row.url = bio.url if bio else None
    row.source = source if bio else None
    row.checked_at = utcnow()


# ------------------------------------------------------------------ top tracks


def store_top_tracks(
    db: Session, artist_id: int, provider: str, tracks: list[tuple[TopTrackData, str | None]]
) -> None:
    """Replace the list: (track, the provider of its preview). The caller commits."""
    db.execute(delete(ArtistTopTrack).where(ArtistTopTrack.artist_id == artist_id))
    now = utcnow()
    for position, (track, preview_provider) in enumerate(tracks[:TOP_LIMIT], start=1):
        db.add(
            ArtistTopTrack(
                artist_id=artist_id,
                position=position,
                title=track.title[:512],
                album_title=(track.album or None) and track.album[:512],
                duration_s=track.duration_s,
                provider=provider,
                track_url=track.track_url,
                preview_url=track.preview_url if preview_provider else None,
                preview_provider=preview_provider if track.preview_url else None,
                fetched_at=now,
            )
        )
    artist = db.get(Artist, artist_id)
    if artist is not None:
        artist.top_checked_at = now


def list_top_tracks(db: Session, artist_id: int) -> list[ArtistTopTrack]:
    return list(
        db.scalars(
            select(ArtistTopTrack)
            .where(ArtistTopTrack.artist_id == artist_id)
            .order_by(ArtistTopTrack.position)
        )
    )


def preview_alive(track: ArtistTopTrack, *, now: datetime | None = None) -> bool:
    """Can the preview be played? Deezer's addresses expire after a few minutes."""
    if not track.preview_url:
        return False
    if track.preview_provider == TOP_DEEZER:
        return (now or utcnow()) - track.fetched_at < DEEZER_PREVIEW_LIFETIME
    return True


def top_state(artist: Artist, tracks: list[ArtistTopTrack], *, now: datetime | None = None) -> str:
    """unknown (never looked up), stale (old list), expired (previews to renew) or ready."""
    now = now or utcnow()
    if artist.top_checked_at is None:
        return "unknown"
    if now - artist.top_checked_at >= TOP_RECHECK:
        return "stale"
    if any(t.preview_url and not preview_alive(t, now=now) for t in tracks):
        return "expired"
    return "ready"
