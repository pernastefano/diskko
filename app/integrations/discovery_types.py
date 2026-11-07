"""Plain results shared by the discovery clients and services."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SimilarHit:
    name: str
    mbid: str | None
    score: float  # 0..1


@dataclass(frozen=True)
class AlbumCandidate:
    provider_id: str
    title: str
    artist: str
    track_count: int | None
    year: int | None = None
    url: str | None = None  # the album's page at the provider (for the attribution link)


@dataclass(frozen=True)
class ArtistCandidate:
    name: str
    picture_url: str | None
    fans: int = 0


@dataclass(frozen=True)
class PreviewData:
    disc: int
    position: int
    title: str
    provider_track_id: str | None
    url: str
