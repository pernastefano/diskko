"""Cover Art Archive client: the front cover of a release group.

GET /release-group/{mbid}/front-250 answers 307 (redirect to archive.org) or 404 when the
community has not chosen a cover. The documentation names no rate limit and no User-Agent
rule, but mentions a 503 for "exceeding the rate limit": we send a User-Agent and keep to
two requests per second.
"""

import re

import httpx

from app.config import Settings
from app.integrations.ratelimit import RateLimiter, get_limiter

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}


class CoverArtError(Exception):
    pass


class CoverArtClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self._http = http
        self._own_http = http is None
        self._limiter = limiter or get_limiter("coverart", settings.coverart_min_interval)

    async def __aenter__(self) -> "CoverArtClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()

    async def fetch_front(
        self, release_group_mbid: str, size: int = 250
    ) -> tuple[bytes, str] | None:
        """(image bytes, file extension), or None when the release group has no cover."""
        if not _UUID.match(release_group_mbid):
            raise ValueError("invalid MBID")
        if size not in (250, 500, 1200):
            raise ValueError("size must be 250, 500 or 1200")
        assert self._http is not None, "use the client as an async context manager"
        url = (
            f"{self.settings.coverart_url.rstrip('/')}/release-group/"
            f"{release_group_mbid}/front-{size}"
        )
        await self._limiter.wait()
        try:
            resp = await self._http.get(
                url, headers={"User-Agent": self.settings.mb_user_agent or "Diskko"}
            )
        except httpx.HTTPError as exc:
            raise CoverArtError(f"Cover Art Archive unreachable ({type(exc).__name__})") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CoverArtError(f"Cover Art Archive answered HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        extension = _IMAGE_TYPES.get(content_type)
        if extension is None:
            raise CoverArtError(f"Unexpected content type {content_type!r}")
        if len(resp.content) > MAX_IMAGE_BYTES:
            raise CoverArtError("Cover image is too large")
        return resp.content, extension
