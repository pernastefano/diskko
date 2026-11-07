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
    cover_url: str | None = None  # a 250 px front cover, when the provider gives one


@dataclass(frozen=True)
class ArtistCandidate:
    name: str
    picture_url: str | None
    fans: int = 0
    provider_id: str | None = None  # the artist's id at the provider


@dataclass(frozen=True)
class TopTrackData:
    title: str
    album: str | None = None
    duration_s: int | None = None
    track_url: str | None = None  # the track at the provider
    preview_url: str | None = None
    artist: str | None = None  # who the provider says plays it (to check a search result)


@dataclass(frozen=True)
class BioData:
    text: str  # plain text, as long as the provider gave it
    url: str | None = None  # the page it comes from, shown as the credit


@dataclass(frozen=True)
class PreviewData:
    disc: int
    position: int
    title: str
    provider_track_id: str | None
    url: str
