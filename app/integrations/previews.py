"""30 second previews: iTunes Search API first, Deezer as the fallback.

iTunes (official docs, performance-partners.apple.com/search-api): /search?term&media=music&
entity=album returns {"resultCount", "results": [{"wrapperType": "collection", "collectionType":
"Album", "collectionId", "collectionName", "artistName", "trackCount", "releaseDate"}]} and
/lookup?id=<collectionId>&entity=song the album followed by its songs ({"wrapperType": "track",
"kind": "song", "trackName", "trackNumber", "discNumber", "previewUrl"}). About 20 calls per
minute are allowed. Previews may only be streamed: never downloaded, saved or cached.

Deezer (its reference pages need a login, so the payloads were checked against live answers):
/search/album?q=artist:"x" album:"y" -> {"data": [{"id", "title", "nb_tracks", "artist": {...}}]},
/album/{id}/tracks -> {"data": [{"id", "title", "track_position", "disk_number", "preview"}]}.
Errors arrive with HTTP 200 as {"error": {"type", "message", "code"}} (800 = no data, 4 = quota).
`preview` URLs are signed and expire after about 15 minutes, so they are never cached here.
The terms allow private, non-commercial use and forbid storing the audio.
"""

import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from app.integrations.cached_http import (
    MISSING,
    OK,
    CachedJsonClient,
    ServiceError,
    TransientError,
)
from app.integrations.discovery_types import AlbumCandidate, ArtistCandidate, PreviewData

SEARCH_TTL = timedelta(days=30)
RESULT_LIMIT = 10


def _year(value: Any) -> int | None:
    match = re.match(r"(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


STORE_HOSTS = ("apple.com", "deezer.com")


def store_url(value: Any) -> str | None:
    """The provider's own page, only if it really is one (it is shown as a link)."""
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    ok = parsed.scheme == "https" and any(host == h or host.endswith("." + h) for h in STORE_HOSTS)
    return str(value) if ok else None


def picture_url(value: Any) -> str | None:
    """A Deezer picture address, only if it is an https address at Deezer's image servers."""
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    ok = parsed.scheme == "https" and (host == "dzcdn.net" or host.endswith(".dzcdn.net"))
    return str(value) if ok else None


class ItunesClient(CachedJsonClient):
    service = "itunes"
    min_interval = 3.2  # documented: about 20 calls per minute

    async def search_albums(self, artist: str, title: str, *, force=False) -> list[AlbumCandidate]:
        data = await self.get_json(
            f"{self.settings.itunes_url.rstrip('/')}/search",
            {
                "term": f"{artist} {title}",
                "media": "music",
                "entity": "album",
                "limit": RESULT_LIMIT,
                "country": self.settings.preview_country.upper(),
            },
            ttl=SEARCH_TTL,
            force=force,
        )
        found = []
        for raw in (data or {}).get("results", []):
            if raw.get("wrapperType") != "collection" or not raw.get("collectionId"):
                continue
            found.append(
                AlbumCandidate(
                    str(raw["collectionId"]),
                    str(raw.get("collectionName") or ""),
                    str(raw.get("artistName") or ""),
                    raw.get("trackCount"),
                    _year(raw.get("releaseDate")),
                    store_url(raw.get("collectionViewUrl")),
                )
            )
        return found

    async def album_tracks(self, collection_id: str, *, force=False) -> list[PreviewData]:
        data = await self.get_json(
            f"{self.settings.itunes_url.rstrip('/')}/lookup",
            {
                "id": collection_id,
                "entity": "song",
                "limit": 200,
                "country": self.settings.preview_country.upper(),
            },
            ttl=SEARCH_TTL,  # iTunes preview addresses do not expire
            force=force,
        )
        tracks = []
        for raw in (data or {}).get("results", []):
            url = raw.get("previewUrl")
            if raw.get("wrapperType") != "track" or raw.get("kind") != "song" or not url:
                continue
            tracks.append(
                PreviewData(
                    int(raw.get("discNumber") or 1),
                    int(raw.get("trackNumber") or len(tracks) + 1),
                    str(raw.get("trackName") or ""),
                    str(raw["trackId"]) if raw.get("trackId") else None,
                    str(url),
                )
            )
        return tracks


class DeezerClient(CachedJsonClient):
    service = "deezer"
    min_interval = 0.25  # no published limit: stay well below any plausible one

    def interpret(self, status: int, data: Any) -> str:
        error = data.get("error") if isinstance(data, dict) else None
        if error:
            code = int(error.get("code") or 0) if isinstance(error, dict) else 0
            if code == 800:
                return MISSING
            if code == 4:  # quota exceeded
                raise TransientError("Deezer quota exceeded")
            raise ServiceError(f"Deezer error {code}")
        return super().interpret(status, data)

    async def search_albums(self, artist: str, title: str, *, force=False) -> list[AlbumCandidate]:
        def clean(text: str) -> str:
            return " ".join(text.replace('"', " ").split())

        data = await self.get_json(
            f"{self.settings.deezer_url.rstrip('/')}/search/album",
            {"q": f'artist:"{clean(artist)}" album:"{clean(title)}"', "limit": RESULT_LIMIT},
            ttl=SEARCH_TTL,
            force=force,
        )
        found = []
        for raw in (data or {}).get("data", []):
            if not raw.get("id"):
                continue
            found.append(
                AlbumCandidate(
                    str(raw["id"]),
                    str(raw.get("title") or ""),
                    str((raw.get("artist") or {}).get("name") or ""),
                    raw.get("nb_tracks"),
                    url=store_url(raw.get("link")),
                    cover_url=picture_url(raw.get("cover_medium")),
                )
            )
        return found

    async def search_artists(self, name: str, *, force=False) -> list[ArtistCandidate]:
        """Artists named like this, with their picture. The answer is NOT sorted by relevance
        (a tribute band can come first): the caller picks by exact name and number of fans."""
        clean = " ".join(name.replace('"', " ").split())
        data = await self.get_json(
            f"{self.settings.deezer_url.rstrip('/')}/search/artist",
            {"q": f'artist:"{clean}"', "limit": RESULT_LIMIT},
            ttl=SEARCH_TTL,
            force=force,
        )
        found = []
        for raw in (data or {}).get("data", []):
            if not raw.get("name"):
                continue
            found.append(
                ArtistCandidate(
                    str(raw["name"]),
                    picture_url(raw.get("picture_medium")),
                    int(raw.get("nb_fan") or 0),
                )
            )
        return found

    async def album_tracks(self, album_id: str) -> list[PreviewData]:
        data = await self.get_json(
            f"{self.settings.deezer_url.rstrip('/')}/album/{album_id}/tracks",
            {"limit": 100},
            ttl=None,  # the signed preview addresses expire: never cached
        )
        tracks = []
        for raw in (data or {}).get("data", []):
            url = raw.get("preview")
            if not url:
                continue
            tracks.append(
                PreviewData(
                    int(raw.get("disk_number") or 1),
                    int(raw.get("track_position") or len(tracks) + 1),
                    str(raw.get("title") or ""),
                    str(raw["id"]) if raw.get("id") else None,
                    str(url),
                )
            )
        return tracks


__all__ = ["DeezerClient", "ItunesClient", "OK"]
