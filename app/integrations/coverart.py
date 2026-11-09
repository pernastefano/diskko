"""Cover Art Archive client: the front cover of a release group.

GET /release-group/{mbid}/front-250 answers 307 (redirect to archive.org) or 404 when the
community has not chosen a cover. The documentation says there are "no rate limiting rules in
place" but lists a 503 for "exceeding the rate limit", so: a User-Agent is sent, requests are
spaced, and on a 503/429 (or a timeout: the image is behind a redirect to archive.org, which is
sometimes slow) the request is tried once more after a pause, `Retry-After` respected, with the
limiter held back for every other job too.
"""

import re

import httpx

from app.config import Settings
from app.integrations import breaker
from app.integrations.ratelimit import RateLimiter, get_limiter

MAX_IMAGE_BYTES = 5 * 1024 * 1024
TRIES = 2  # one try and one more
RETRY_PAUSE = 3.0  # seconds before the second try when the service does not say
INLINE_WAIT = 10.0  # a longer pause is left to the caller (the job tries again later)
RETRY_AFTER_CAP = 60.0
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}


SERVICE = "coverart"


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
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True
            )
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
        breaker.check(self.settings, SERVICE)
        headers = {"User-Agent": self.settings.mb_user_agent or "Diskko"}
        problem = ""
        for attempt in range(TRIES):
            await self._limiter.wait()
            pause = RETRY_PAUSE
            try:
                resp = await self._http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                problem = f"Cover Art Archive unreachable ({type(exc).__name__})"
            else:
                if resp.status_code < 500 and resp.status_code != 429:
                    break
                problem = f"Cover Art Archive answered HTTP {resp.status_code}"
                pause = _retry_after(resp.headers.get("Retry-After"), RETRY_PAUSE)
            self._limiter.penalize(pause)  # nobody asks the service during the pause
            if attempt + 1 == TRIES or pause > INLINE_WAIT:
                breaker.record_failure(self.settings, SERVICE)
                raise CoverArtError(problem)
        breaker.record_success(SERVICE)
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


def _retry_after(value: str | None, default: float) -> float:
    try:
        return max(default, min(float(value), RETRY_AFTER_CAP)) if value else default
    except ValueError:
        return default
