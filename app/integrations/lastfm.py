"""Last.fm `artist.getSimilar` (official docs: https://www.last.fm/api/show/artist.getSimilar).

Read only: it needs an API key but no session. Answers are JSON when `format=json`:
{"similarartists": {"artist": [{"name", "mbid", "match": "0.51", "url", "image": [...]}]}}.
Errors come as {"error": <code>, "message": ...}: 6 = artist not found, 10 = invalid key,
26 = suspended key, 29 = rate limit, 8/11/16 = temporary. The terms ask for an identifiable
User-Agent, credit to Last.fm and "reasonable" use (no published numeric limit).
"""

import html
import re
from datetime import timedelta
from typing import Any

from app.integrations.cached_http import (
    MISSING,
    OK,
    CachedJsonClient,
    NotConfigured,
    ServiceError,
    TransientError,
)
from app.integrations.discovery_types import BioData, SimilarHit, TopTrackData

SIMILAR_TTL = timedelta(days=14)
TOP_TTL = timedelta(days=7)
BIO_TTL = timedelta(days=30)
_TAGS = re.compile(r"<[^>]+>")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TEMPORARY = {8, 11, 16, 17, 29}


class LastFmClient(CachedJsonClient):
    service = "lastfm"
    min_interval = 0.5  # the terms say "reasonable"; two per second at most

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not self.settings.lastfm_api_key.strip():
            raise NotConfigured("the Last.fm API key is not set")

    def interpret(self, status: int, data: Any) -> str:
        if isinstance(data, dict) and "error" in data:
            code = int(data.get("error") or 0)
            if code == 6:
                return MISSING
            if code in _TEMPORARY:
                raise TransientError(f"Last.fm error {code}")
            if code in (10, 26):
                raise ServiceError("Last.fm refused the API key (invalid or suspended)")
            raise ServiceError(f"Last.fm error {code}")
        if status == 200:
            return OK
        raise ServiceError(f"unexpected answer from Last.fm (HTTP {status})")

    async def similar(
        self, *, mbid: str | None = None, name: str | None = None, limit: int = 30, force=False
    ) -> list[SimilarHit]:
        if not (mbid or name):
            return []
        params: dict[str, Any] = {
            "method": "artist.getsimilar",
            "limit": limit,
            "autocorrect": 1,
            "format": "json",
            "api_key": self.settings.lastfm_api_key.strip(),
        }
        params["mbid" if mbid else "artist"] = mbid or name
        data = await self.get_json(
            self.settings.lastfm_url,
            params,
            ttl=SIMILAR_TTL,
            secret_params=("api_key",),
            force=force,
        )
        return parse_similar(data)

    async def top_tracks(
        self, *, mbid: str | None = None, name: str | None = None, limit: int = 10, force=False
    ) -> list[TopTrackData]:
        """artist.getTopTracks (https://www.last.fm/api/show/artist.getTopTracks): the list only,
        there are no previews."""
        if not (mbid or name):
            return []
        params = _artist_params(
            "artist.gettoptracks", mbid, name, self.settings.lastfm_api_key.strip()
        )
        params["limit"] = limit
        data = await self.get_json(
            self.settings.lastfm_url,
            params,
            ttl=TOP_TTL,
            secret_params=("api_key",),
            force=force,
        )
        return parse_top_tracks(data)[:limit]

    async def bio(
        self, *, mbid: str | None = None, name: str | None = None, lang: str = "en", force=False
    ) -> BioData | None:
        """artist.getInfo (https://www.last.fm/api/show/artist.getInfo), `lang` is ISO 639-1."""
        if not (mbid or name):
            return None
        params = _artist_params("artist.getinfo", mbid, name, self.settings.lastfm_api_key.strip())
        params["lang"] = lang
        data = await self.get_json(
            self.settings.lastfm_url,
            params,
            ttl=BIO_TTL,
            secret_params=("api_key",),
            force=force,
        )
        return parse_bio(data)


def _artist_params(method: str, mbid: str | None, name: str | None, key: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "method": method,
        "autocorrect": 1,
        "format": "json",
        "api_key": key,
    }
    params["mbid" if mbid else "artist"] = mbid or name
    return params


def parse_top_tracks(data: Any) -> list[TopTrackData]:
    """artist.getTopTracks: toptracks.track[] with name, playcount, listeners, mbid, url, in
    rank order (the docs show the XML elements; the JSON uses the same names)."""
    block = (data or {}).get("toptracks") if isinstance(data, dict) else None
    items = (block or {}).get("track") or []
    if isinstance(items, dict):
        items = [items]
    tracks = []
    for raw in items:
        title = str(raw.get("name") or "").strip()
        if title:
            url = str(raw.get("url") or "")
            tracks.append(TopTrackData(title, track_url=url if _lastfm_url(url) else None))
    return tracks


def _lastfm_url(value: str) -> bool:
    return value.startswith("https://www.last.fm/") or value.startswith("http://www.last.fm/")


def parse_bio(data: Any) -> BioData | None:
    """artist.getInfo: artist.bio.summary (and content), truncated by Last.fm; it may carry
    HTML and a "Read more" link, which are removed here. The credit is the artist's page."""
    artist = (data or {}).get("artist") if isinstance(data, dict) else None
    bio = (artist or {}).get("bio") or {}
    raw = str(bio.get("summary") or bio.get("content") or "")
    text = " ".join(html.unescape(_TAGS.sub(" ", raw)).split())
    text = re.sub(r"\s*Read more on Last\.fm\.?$", "", text).strip()
    if not text:
        return None
    url = str((artist or {}).get("url") or "")
    return BioData(text, url if _lastfm_url(url) else None)


def parse_similar(data: Any) -> list[SimilarHit]:
    block = (data or {}).get("similarartists") if isinstance(data, dict) else None
    items = (block or {}).get("artist") or []
    if isinstance(items, dict):  # one result is sometimes a bare object
        items = [items]
    hits: list[SimilarHit] = []
    for raw in items:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        try:
            score = max(0.0, min(1.0, float(raw.get("match") or 0)))
        except (TypeError, ValueError):
            score = 0.0
        mbid = str(raw.get("mbid") or "").lower()
        hits.append(SimilarHit(name, mbid if _UUID.match(mbid) else None, score))
    return hits
