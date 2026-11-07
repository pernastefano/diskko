"""Where a front cover can come from: one class per provider, all with the same interface.

`fetch` returns (image bytes, extension), None when this provider has no such cover, and
raises ServiceError when it could not answer (the next provider is then tried) or
ServiceBreakerOpen when the service is paused after repeated failures.

Cover Art Archive is exact (it is asked by MusicBrainz release group id). Deezer is a search by
artist and title: only a candidate that passes the same strict match as the previews is used,
a wrong cover is worse than none.
"""

from typing import Protocol

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.integrations import breaker
from app.integrations.cached_http import ServiceError
from app.integrations.coverart import MAX_IMAGE_BYTES, CoverArtClient, CoverArtError
from app.integrations.previews import DeezerClient
from app.integrations.ratelimit import get_limiter
from app.services.previews import best_candidate

LABELS = {"coverartarchive": "Cover Art Archive", "deezer": "Deezer"}
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}


class CoverSource(Protocol):
    name: str

    async def __aenter__(self) -> "CoverSource": ...

    async def __aexit__(self, *exc: object) -> None: ...

    async def fetch(
        self, mbid: str, artist: str, title: str, track_hint: int | None
    ) -> tuple[bytes, str] | None: ...


class CoverArtArchiveSource:
    name = "coverartarchive"
    service = "coverart"

    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]) -> None:
        self._client = CoverArtClient(settings)

    async def __aenter__(self) -> "CoverArtArchiveSource":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    async def fetch(self, mbid, artist, title, track_hint):
        try:
            return await self._client.fetch_front(mbid)
        except ValueError:  # not a valid MBID: nothing to look up
            return None
        except CoverArtError as exc:
            raise ServiceError(str(exc)) from exc


class DeezerCoverSource:
    name = "deezer"
    service = "deezer"

    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]) -> None:
        self._settings = settings
        self._search = DeezerClient(settings, session_factory)
        self._http: httpx.AsyncClient | None = None
        self._limiter = get_limiter("deezer", DeezerClient.min_interval)

    async def __aenter__(self) -> "DeezerCoverSource":
        await self._search.__aenter__()
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._search.__aexit__(*exc)
        if self._http is not None:
            await self._http.aclose()

    async def fetch(self, mbid, artist, title, track_hint):
        candidates = await self._search.search_albums(artist, title)
        pick = best_candidate(candidates, artist, title, track_hint)
        if pick is None or not pick.cover_url:
            return None
        assert self._http is not None, "use the source as an async context manager"
        breaker.check(self._settings, self.service)
        await self._limiter.wait()
        try:
            resp = await self._http.get(pick.cover_url)
        except httpx.HTTPError as exc:
            breaker.record_failure(self._settings, self.service)
            raise ServiceError(f"Deezer unreachable ({type(exc).__name__})") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            breaker.record_failure(self._settings, self.service)
            raise ServiceError(f"Deezer answered HTTP {resp.status_code}")
        breaker.record_success(self.service)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ServiceError(f"Deezer answered HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        extension = _IMAGE_TYPES.get(content_type)
        if extension is None:
            raise ServiceError(f"Unexpected content type {content_type!r}")
        if len(resp.content) > MAX_IMAGE_BYTES:
            raise ServiceError("Cover image is too large")
        return resp.content, extension


SOURCES: dict[str, type] = {"coverartarchive": CoverArtArchiveSource, "deezer": DeezerCoverSource}
