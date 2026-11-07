"""What the artist page shows besides the albums: a short biography and the top tracks."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

# ArtistBio.source
BIO_WIKIPEDIA = "wikipedia"
BIO_LASTFM = "lastfm"

# ArtistTopTrack.provider / preview_provider
TOP_DEEZER = "deezer"
TOP_LASTFM = "lastfm"
TOP_ITUNES = "itunes"


class ArtistBio(Base):
    """The biography of an artist in one language. An empty text means "looked up, none"."""

    __tablename__ = "artist_bios"
    __table_args__ = (UniqueConstraint("artist_id", "lang"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    lang: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)  # the page it comes from
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArtistTopTrack(Base):
    """One of the most played tracks of an artist, with a 30 second preview address if any."""

    __tablename__ = "artist_top_tracks"
    __table_args__ = (UniqueConstraint("artist_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512))
    album_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str] = mapped_column(String(16))  # where the list comes from
    track_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # the track at the provider
    preview_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
