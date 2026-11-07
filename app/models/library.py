from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow

SOURCE_FILESYSTEM = "filesystem"
SOURCE_PLEX = "plex"


class Artist(Base):
    __tablename__ = "artists"
    __table_args__ = (Index("ix_artists_name_key", "name_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mbid: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    sort_name: Mapped[str] = mapped_column(String(255))
    # Normalized name (case, accents, spacing) used to merge artists without an MBID.
    name_key: Mapped[str] = mapped_column(String(255))
    # Filled by the MusicBrainz sync (phase 4).
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    disambiguation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # MusicBrainz identity (see models/musicbrainz.py for the statuses).
    mb_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'")
    )
    mb_source: Mapped[str | None] = mapped_column(String(8), nullable=True)  # tag|auto|manual
    mb_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Fallback metadata provider (when the artist is not on MusicBrainz) and its artist id
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provider_artist_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    in_library: Mapped[bool] = mapped_column(Boolean, default=True)
    # The artist's picture (an address at Deezer, not a copy) and when it was last looked up.
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    albums: Mapped[list["LibraryAlbum"]] = relationship(
        back_populates="artist", cascade="all, delete-orphan", passive_deletes=True
    )


class LibraryAlbum(Base):
    __tablename__ = "library_albums"
    # A folder can hold several albums (e.g. a "Singles" folder): path alone is not unique.
    __table_args__ = (UniqueConstraint("source", "path", "title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_FILESYSTEM)
    # Folder of the album, relative to the library root (POSIX style).
    path: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String(16), nullable=True)  # dominant codec
    quality_tier: Mapped[str] = mapped_column(String(48), default="any")
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)  # average, kbps
    track_count: Mapped[int] = mapped_column(Integer, default=0)
    disc_count: Mapped[int] = mapped_column(Integer, default=1)
    total_size: Mapped[int] = mapped_column(BigInteger, default=0)
    plex_rating_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mbid_release: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mbid_release_group: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_compilation: Mapped[bool] = mapped_column(Boolean, default=False)
    # Link to MusicBrainz (filled by the matching step, see services/musicbrainz/matching.py).
    release_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("release_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    matched_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_status: Mapped[str] = mapped_column(
        String(16), default="unmatched", server_default=text("'unmatched'")
    )
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    artist: Mapped[Artist] = relationship(back_populates="albums")
    tracks: Mapped[list["LibraryTrack"]] = relationship(
        back_populates="album", cascade="all, delete-orphan", passive_deletes=True
    )


class LibraryTrack(Base):
    __tablename__ = "library_tracks"
    __table_args__ = (UniqueConstraint("source", "path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    album_id: Mapped[int] = mapped_column(
        ForeignKey("library_albums.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_FILESYSTEM)
    path: Mapped[str] = mapped_column(String(1024))  # relative to the library root
    # Used by the incremental scan: unchanged files are not read again.
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    file_mtime_ns: Mapped[int] = mapped_column(BigInteger, default=0)
    title: Mapped[str] = mapped_column(String(512))
    track_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disc_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    codec: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lossless: Mapped[bool] = mapped_column(Boolean, default=False)
    v0: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))  # LAME V0
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)  # kbps
    bit_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)  # seconds
    quality_tier: Mapped[str] = mapped_column(String(48), default="any")
    # Tags kept to group tracks into albums without reading the files again.
    artist: Mapped[str | None] = mapped_column(String(255), nullable=True)
    album_artist: Mapped[str | None] = mapped_column(String(255), nullable=True)
    album_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artist_sort: Mapped[str | None] = mapped_column(String(255), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compilation: Mapped[bool] = mapped_column(Boolean, default=False)
    mbid_recording: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mbid_artist: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mbid_album_artist: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mbid_release: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mbid_release_group: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plex_rating_key: Mapped[str | None] = mapped_column(String(32), nullable=True)

    album: Mapped[LibraryAlbum] = relationship(back_populates="tracks")
