"""Discovery: similar artists and album previews (results of external lookups)."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

# SimilarArtist.source / SimilarFetch.source
SIMILAR_LASTFM = "lastfm"
SIMILAR_LISTENBRAINZ = "listenbrainz"

# PreviewLookup.provider / status
PREVIEW_ITUNES = "itunes"
PREVIEW_DEEZER = "deezer"
PREVIEW_FOUND = "found"
PREVIEW_NONE = "none"


class SimilarArtist(Base):
    __tablename__ = "similar_artists"
    __table_args__ = (Index("ix_similar_artists_artist_position", "artist_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    name_key: Mapped[str] = mapped_column(String(255))
    mbid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1, best first
    source: Mapped[str] = mapped_column(String(16))
    position: Mapped[int] = mapped_column(Integer, default=0)


class SimilarFetch(Base):
    """When the similar artists of an artist were last looked up (even if there were none)."""

    __tablename__ = "similar_fetches"

    artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class PreviewLookup(Base):
    """Which provider has previews for a release group (or that none has)."""

    __tablename__ = "preview_lookups"

    release_group_id: Mapped[int] = mapped_column(
        ForeignKey("release_groups.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provider_album_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # the album at the store
    status: Mapped[str] = mapped_column(String(8), default=PREVIEW_NONE)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PreviewTrack(Base):
    """A 30 second preview. Only the address is kept: the audio is never downloaded."""

    __tablename__ = "preview_tracks"
    __table_args__ = (UniqueConstraint("release_group_id", "disc", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_group_id: Mapped[int] = mapped_column(
        ForeignKey("release_groups.id", ondelete="CASCADE"), index=True
    )
    disc: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512))
    provider_track_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReleaseTrack(Base):
    """A track of an album as MusicBrainz lists it (from one chosen release of the group)."""

    __tablename__ = "release_tracks"
    __table_args__ = (UniqueConstraint("release_group_id", "disc", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_group_id: Mapped[int] = mapped_column(
        ForeignKey("release_groups.id", ondelete="CASCADE"), index=True
    )
    disc: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer)
    number: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "A1" on vinyl
    title: Mapped[str] = mapped_column(String(512))
    length_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recording_mbid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # who plays the track: [{"name", "mbid", "join"}], as MusicBrainz credits it (compilations)
    artist_credit_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_mbid: Mapped[str | None] = mapped_column(String(36), nullable=True)


class TracklistFetch(Base):
    """When the tracks of a release group were last looked up (even if none were found)."""

    __tablename__ = "tracklist_fetches"

    release_group_id: Mapped[int] = mapped_column(
        ForeignKey("release_groups.id", ondelete="CASCADE"), primary_key=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    release_mbid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    track_count: Mapped[int] = mapped_column(Integer, default=0)
