"""Last.fm `artist.getSimilar` (official docs: https://www.last.fm/api/show/artist.getSimilar).

Read only: it needs an API key but no session. Answers are JSON when `format=json`:
{"similarartists": {"artist": [{"name", "mbid", "match": "0.51", "url", "image": [...]}]}}.
Errors come as {"error": <code>, "message": ...}: 6 = artist not found, 10 = invalid key,
26 = suspended key, 29 = rate limit, 8/11/16 = temporary. The terms ask for an identifiable
User-Agent, credit to Last.fm and "reasonable" use (no published numeric limit).
"""

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
from app.integrations.discovery_types import SimilarHit

SIMILAR_TTL = timedelta(days=14)
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
