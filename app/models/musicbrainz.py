from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

# Artist.mb_status
MB_PENDING = "pending"  # not looked up yet
MB_RESOLVED = "resolved"  # has an MBID
MB_REVIEW = "review"  # waiting for a manual decision
MB_UNMATCHED = "unmatched"  # decided: not on MusicBrainz
MB_IGNORED = "ignored"  # never looked up (e.g. "Unknown Artist")
MB_PROVIDER = "provider"  # not on MusicBrainz: metadata comes from a fallback provider
MB_STATUSES = (MB_PENDING, MB_RESOLVED, MB_REVIEW, MB_UNMATCHED, MB_IGNORED, MB_PROVIDER)

# ReleaseGroup.source: where the album information comes from
SOURCE_MUSICBRAINZ = "musicbrainz"
SOURCE_DEEZER = "deezer"

# LibraryAlbum.match_status
MATCH_MATCHED = "matched"  # confident automatic match
MATCH_TENTATIVE = "tentative"  # probable match, to be reviewed (counts as owned)
MATCH_UNMATCHED = "unmatched"
MATCH_MANUAL = "manual"  # chosen by an admin
MATCH_NONE = "none"  # an admin said: not on MusicBrainz
MATCH_STATUSES = (MATCH_MATCHED, MATCH_TENTATIVE, MATCH_UNMATCHED, MATCH_MANUAL, MATCH_NONE)

# ArtistReview.reason / status
REVIEW_PENDING = "pending"
REVIEW_RESOLVED = "resolved"
REVIEW_DISMISSED = "dismissed"


class ApiCache(Base):
    """Cached answers of external APIs (MusicBrainz), so repeated syncs cost no requests."""

    __tablename__ = "api_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[int] = mapped_column(Integer, default=200)  # 200, or 404 (negative cache)
    response: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ttl_seconds: Mapped[int] = mapped_column(Integer)


class ReleaseGroup(Base):
    __tablename__ = "release_groups"
    # A collaboration appears in the discography of each credited artist: unique per artist.
    __table_args__ = (
        UniqueConstraint("artist_id", "mbid"),
        Index("ix_release_groups_artist_category", "artist_id", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mbid: Mapped[str] = mapped_column(String(36), index=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(512))
    primary_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    secondary_types_json: Mapped[str] = mapped_column(Text, default="[]")
    # Partial dates are normal in MusicBrainz: "1968", "1968-03", "1968-03-22".
    first_release_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disambiguation: Mapped[str] = mapped_column(String(255), default="")
    # musicbrainz, or a fallback provider (then `mbid` holds "<provider>:<id>", not an MBID)
    source: Mapped[str] = mapped_column(
        String(16), default="musicbrainz", server_default=text("'musicbrainz'")
    )
    # Single user-facing type derived from primary/secondary types (see categories.py).
    category: Mapped[str] = mapped_column(String(24), default="other")
    track_count_hint: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_status: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown|ok|missing
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArtistReview(Base):
    """Queue of artists whose MusicBrainz identity needs a human decision."""

    __tablename__ = "artist_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(16))  # ambiguous|low_score|no_match|invalid_mbid
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default=REVIEW_PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AlbumMatchOverride(Base):
    """A manual album decision that survives rescans (keyed by artist + normalized title)."""

    __tablename__ = "album_match_overrides"
    __table_args__ = (UniqueConstraint("artist_id", "album_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    album_key: Mapped[str] = mapped_column(String(512))
    # NULL = "this album is not on MusicBrainz".
    release_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("release_groups.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AlbumArtistOverride(Base):
    """ "This album belongs to that artist": a manual decision that survives rescans.

    A scan names the artist of an album by what the tags (or Plex) say, so two artists with the
    same or a similar name can end up as one. The album is told by where it is (its folder) and
    its title, the same two things a scan identifies it by."""

    __tablename__ = "album_artist_overrides"
    __table_args__ = (UniqueConstraint("path", "title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512))
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


__all__ = [
    "AlbumArtistOverride",
    "MATCH_MANUAL",
    "MATCH_MATCHED",
    "MATCH_NONE",
    "MATCH_STATUSES",
    "MATCH_TENTATIVE",
    "MATCH_UNMATCHED",
    "MB_IGNORED",
    "MB_PENDING",
    "MB_PROVIDER",
    "MB_RESOLVED",
    "MB_REVIEW",
    "MB_STATUSES",
    "MB_UNMATCHED",
    "REVIEW_DISMISSED",
    "REVIEW_PENDING",
    "REVIEW_RESOLVED",
    "SOURCE_DEEZER",
    "SOURCE_MUSICBRAINZ",
    "AlbumMatchOverride",
    "ApiCache",
    "ArtistReview",
    "ReleaseGroup",
]
