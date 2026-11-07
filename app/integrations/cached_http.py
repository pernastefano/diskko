"""Base of the read-only discovery clients: cached in `api_cache`, rate limited, retrying.

Cache keys never contain credentials, and error messages never contain URLs (a Last.fm URL
carries the API key). Subclasses decide what an answer means through `interpret`.
"""

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app import __version__
from app.config import Settings
from app.integrations.ratelimit import RateLimiter, get_limiter
from app.models import ApiCache, utcnow

NEGATIVE_TTL = timedelta(days=3)  # "not found" is remembered, but not for long
RETRY_AFTER_CAP = 60.0
OK = "ok"
MISSING = "missing"


class ServiceError(Exception):
    """The service refused or failed for good (retrying will not help)."""


class NotConfigured(ServiceError):
    """The integration lacks something it needs (a key, a contact in the User-Agent)."""


class TransientError(Exception):
    """A temporary failure: the request is retried with a growing delay."""


class CachedJsonClient:
    service = ""  # name of the rate limiter and prefix of the cache keys
    min_interval = 1.0  # seconds between requests
    BACKOFF_BASE = 2.0  # first retry delay in seconds; it doubles at every attempt

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        http: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], Any] = utcnow,
        max_retries: int = 2,
    ) -> None:
        self.settings = settings
        self._factory = session_factory
        self._http = http
        self._own_http = http is None
        self._limiter = limiter or get_limiter(self.service, self.min_interval)
        self._sleep = sleep
        self._now = now
        self._max_retries = max_retries
        self.requests_made = 0  # network requests (cache hits are not counted)

    async def __aenter__(self):
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()

    @property
    def user_agent(self) -> str:
        """The MusicBrainz contact User-Agent when set (it identifies the app and a contact)."""
        return self.settings.mb_user_agent.strip() or f"Diskko/{__version__}"

    # ------------------------------------------------------------------ cache

    def _key(self, url: str, params: dict[str, Any]) -> str:
        raw = json.dumps([self.service, url, sorted(params.items())], default=str)
        return f"{self.service}:{hashlib.sha1(raw.encode()).hexdigest()}"

    def _cache_get(self, key: str) -> tuple[int, str] | None:
        with self._factory() as db:
            row = db.get(ApiCache, key)
            if row is None or row.fetched_at + timedelta(seconds=row.ttl_seconds) <= self._now():
                return None
            return row.status, row.response

    def _cache_put(self, key: str, status: int, body: str, ttl: timedelta) -> None:
        with self._factory() as db:
            row = db.get(ApiCache, key) or ApiCache(key=key)
            row.status, row.response = status, body
            row.fetched_at, row.ttl_seconds = self._now(), int(ttl.total_seconds())
            db.add(row)
            db.commit()

    # ---------------------------------------------------------------- request

    def interpret(self, status: int, data: Any) -> str:
        """OK, MISSING (cache the absence) or raise ServiceError / TransientError."""
        if status == 200:
            return OK
        if status == 404:
            return MISSING
        raise ServiceError(f"unexpected answer (HTTP {status})")

    async def get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        ttl: timedelta | None,
        secret_params: tuple[str, ...] = (),
        force: bool = False,
    ) -> Any | None:
        """GET a JSON document; None when the service says the thing does not exist.

        `ttl=None` skips the cache (for answers that expire quickly, like signed URLs).
        `secret_params` are sent but left out of the cache key.
        """
        key = self._key(url, {k: v for k, v in params.items() if k not in secret_params})
        if ttl is not None and not force:
            cached = await asyncio.to_thread(self._cache_get, key)
            if cached is not None:
                status, body = cached
                return None if status == 404 else json.loads(body)

        assert self._http is not None, "use the client as an async context manager"
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        last = "no attempt"
        for attempt in range(self._max_retries + 1):
            await self._limiter.wait()
            self.requests_made += 1
            try:
                resp = await self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last = type(exc).__name__  # never the message: it can contain the URL
                await self._backoff(attempt, None)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                await self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            try:
                data = resp.json()
            except ValueError:
                data = None
            try:
                outcome = self.interpret(resp.status_code, data)
            except TransientError as exc:
                last = str(exc) or "temporary error"
                await self._backoff(attempt, None)
                continue
            if outcome == MISSING:
                if ttl is not None:
                    await asyncio.to_thread(self._cache_put, key, 404, "", NEGATIVE_TTL)
                return None
            if data is None:
                raise ServiceError("invalid JSON in the answer")
            if ttl is not None:
                await asyncio.to_thread(self._cache_put, key, 200, resp.text, ttl)
            return data
        raise ServiceError(f"{self.service} is not answering ({last}) after retries")

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.BACKOFF_BASE * (2**attempt)
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(retry_after), RETRY_AFTER_CAP))
        self._limiter.penalize(delay)
