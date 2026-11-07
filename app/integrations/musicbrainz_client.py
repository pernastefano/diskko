"""MusicBrainz web service client: rate limited, retrying and cached in `api_cache`.

Facts from the official documentation (see PLAN section 1 / phase 4 notes):
- at most one request per second per client; above that *all* requests get HTTP 503;
- every request needs a meaningful User-Agent;
- JSON with `fmt=json`; browse and search accept `limit` up to 100 and `offset`;
- release groups of an artist: GET /release-group?artist=<MBID> (browse).
The browse response keys (`release-group-count`, `release-groups`) were confirmed against
the live service when the test fixtures were recorded.
"""

import asyncio
import contextlib
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.integrations import breaker
from app.integrations.ratelimit import RateLimiter, get_limiter
from app.models import ApiCache, utcnow
from app.services.musicbrainz.categories import category_for, year_of

PAGE_SIZE = 100
MAX_PAGES = 200  # safety net: 20,000 release groups
NEGATIVE_TTL = timedelta(days=1)
RETRY_AFTER_CAP = 60.0


class MusicBrainzError(Exception):
    """The service could not be used (bad request, too many failures, bad answer)."""


class MusicBrainzNotConfigured(MusicBrainzError):
    """MB_USER_AGENT is empty: MusicBrainz requires an identifying User-Agent."""


@dataclass
class ArtistHit:
    mbid: str
    name: str
    sort_name: str = ""
    score: int = 0  # MusicBrainz search score, 0-100
    country: str | None = None
    disambiguation: str = ""
    type: str | None = None
    aliases: list[str] = field(default_factory=list)


@dataclass
class ReleaseGroupData:
    mbid: str
    title: str
    primary_type: str | None
    secondary_types: list[str]
    first_release_date: str | None
    disambiguation: str = ""

    @property
    def year(self) -> int | None:
        return year_of(self.first_release_date)

    @property
    def category(self) -> str:
        return category_for(self.primary_type, self.secondary_types)


@dataclass
class TrackData:
    disc: int
    position: int
    number: str | None
    title: str
    length_ms: int | None
    recording_mbid: str | None
    # (credited name, artist MBID, join phrase) in the order MusicBrainz lists them
    credits: list[tuple[str, str | None, str]] = field(default_factory=list)


@dataclass
class ReleaseData:
    mbid: str
    date: str | None
    country: str | None
    status: str | None
    tracks: list[TrackData]


def lucene_phrase(text: str) -> str:
    """Quote a value for a Lucene phrase query (only `\\` and `"` are special inside)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cache_key(path: str, params: dict[str, Any]) -> str:
    canonical = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(canonical.encode()).hexdigest()


def search_cache_key(name: str, limit: int) -> str:
    """Cache key of an artist search (the same one `search_artists` writes)."""
    return cache_key(
        "artist", {"query": f"artist:{lucene_phrase(name)}", "limit": limit, "fmt": "json"}
    )


def peek_artist_search(
    db: Session, name: str, limit: int, *, now: Callable[[], Any] = utcnow
) -> list[ArtistHit] | None:
    """The cached answer of an artist search, or None when it is missing or has expired.

    Reading the cache is a local query, so pages can use it; asking MusicBrainz is a job.
    """
    row = db.get(ApiCache, search_cache_key(name, limit))
    if row is None or row.fetched_at + timedelta(seconds=row.ttl_seconds) <= now():
        return None
    if row.status == 404 or not row.response:
        return []
    data = json.loads(row.response)
    return [hit for raw in data.get("artists", []) if (hit := _parse_artist(raw))]


class MusicBrainzClient:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        http: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], Any] = utcnow,
    ) -> None:
        if not settings.mb_user_agent.strip():
            raise MusicBrainzNotConfigured(
                "MB_USER_AGENT is not set: MusicBrainz requires a User-Agent that identifies "
                "the application and a contact, e.g. 'Diskko/0.1 ( you@example.com )'"
            )
        self.settings = settings
        self._factory = session_factory
        self._http = http
        self._own_http = http is None
        self._limiter = limiter or get_limiter("musicbrainz", settings.mb_min_interval)
        self._sleep = sleep
        self._now = now
        self.requests_made = 0  # network requests (cache hits not counted)

    async def __aenter__(self) -> "MusicBrainzClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------- cache

    def _cache_get(self, key: str) -> tuple[int, str] | None:
        with self._factory() as db:
            row = db.get(ApiCache, key)
            if row is None:
                return None
            if row.fetched_at + timedelta(seconds=row.ttl_seconds) <= self._now():
                return None
            return row.status, row.response

    def _cache_put(self, key: str, status: int, body: str, ttl: timedelta) -> None:
        with self._factory() as db:
            row = db.get(ApiCache, key) or ApiCache(key=key)
            row.status, row.response = status, body
            row.fetched_at, row.ttl_seconds = self._now(), int(ttl.total_seconds())
            db.add(row)
            db.commit()

    # ----------------------------------------------------------------- request

    async def _get(
        self, path: str, params: dict[str, Any], *, force: bool = False
    ) -> dict[str, Any] | None:
        """GET a JSON document; None when the service answers 404."""
        params = {**params, "fmt": "json"}
        key = cache_key(path, params)
        if not force:
            cached = await asyncio.to_thread(self._cache_get, key)
            if cached is not None:
                status, body = cached
                return None if status == 404 else json.loads(body)

        assert self._http is not None, "use the client as an async context manager"
        breaker.check(self.settings, "musicbrainz")  # paused: the job waits (RetryLater)
        url = f"{self.settings.musicbrainz_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {"User-Agent": self.settings.mb_user_agent, "Accept": "application/json"}
        ttl = timedelta(days=self.settings.mb_cache_ttl_days)
        last = "no attempt"
        for attempt in range(self.settings.mb_max_retries + 1):
            await self._limiter.wait()
            self.requests_made += 1
            try:
                resp = await self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last = type(exc).__name__  # never the message: it can contain the URL
                await self._backoff(attempt, None)
                continue
            if resp.status_code < 500 and resp.status_code != 429:
                breaker.record_success("musicbrainz")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise MusicBrainzError("MusicBrainz returned invalid JSON") from exc
                await asyncio.to_thread(self._cache_put, key, 200, resp.text, ttl)
                return data
            if resp.status_code == 404:
                await asyncio.to_thread(self._cache_put, key, 404, "", NEGATIVE_TTL)
                return None
            if resp.status_code == 400:
                raise MusicBrainzError("MusicBrainz rejected the request (HTTP 400)")
            last = f"HTTP {resp.status_code}"
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                await self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            raise MusicBrainzError(f"Unexpected answer from MusicBrainz (HTTP {resp.status_code})")
        breaker.record_failure(self.settings, "musicbrainz")
        raise MusicBrainzError(f"MusicBrainz is not answering ({last}) after retries")

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = 2.0 * (2**attempt)
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(retry_after), RETRY_AFTER_CAP))
        # 503 means we were over the limit: the limiter must stay quiet for a while.
        self._limiter.penalize(delay)

    # ------------------------------------------------------------------ queries

    async def search_artists(
        self, name: str, *, limit: int = 10, force: bool = False
    ) -> list[ArtistHit]:
        data = await self._get(
            "artist", {"query": f"artist:{lucene_phrase(name)}", "limit": limit}, force=force
        )
        return [hit for raw in (data or {}).get("artists", []) if (hit := _parse_artist(raw))]

    async def lookup_artist(self, mbid: str, *, force: bool = False) -> ArtistHit | None:
        data = await self._get(f"artist/{mbid}", {}, force=force)
        return _parse_artist(data) if data else None

    async def artist_wikidata(self, mbid: str, *, force: bool = False) -> str | None:
        """The artist's Wikidata id (`inc=url-rels`: relations[] {type, url {resource}})."""
        from app.integrations.wikipedia import wikidata_id

        data = await self._get(f"artist/{mbid}", {"inc": "url-rels"}, force=force)
        return wikidata_id((data or {}).get("relations") or [])

    async def browse_releases(
        self, release_group_mbid: str, *, force: bool = False
    ) -> list[ReleaseData] | None:
        """The releases of a release group with their tracks (official ones when there are any).

        GET /release?release-group=<mbid>&inc=recordings: each release has media[], each medium
        tracks[] {position, number, title, length (ms), recording {id}}. None if MusicBrainz does
        not know the group. One page of 25 is plenty: only a representative release is needed.
        """
        for status in ("official", None):
            params: dict[str, Any] = {
                "release-group": release_group_mbid,
                "inc": "recordings artist-credits",  # a space becomes the "+" MusicBrainz wants
                "limit": 25,
            }
            if status:
                params["status"] = status
            data = await self._get("release", params, force=force)
            if data is None:
                return None
            releases = [r for raw in data.get("releases", []) if (r := _parse_release(raw))]
            if releases:
                return releases
        return []

    async def lookup_release_group(
        self, mbid: str, *, force: bool = False
    ) -> ReleaseGroupData | None:
        """One release group (GET /release-group/{mbid}); None if MusicBrainz does not know it."""
        data = await self._get(f"release-group/{mbid}", {}, force=force)
        return _parse_release_group(data) if data else None

    async def browse_release_groups(
        self,
        artist_mbid: str,
        *,
        force: bool = False,
        on_page: Callable[[int, int], Any] | None = None,
    ) -> list[ReleaseGroupData] | None:
        """Every release group of an artist (all types). None if the artist does not exist."""
        groups: list[ReleaseGroupData] = []
        offset = 0
        for _ in range(MAX_PAGES):
            data = await self._get(
                "release-group",
                {"artist": artist_mbid, "limit": PAGE_SIZE, "offset": offset},
                force=force,
            )
            if data is None:
                return None
            page = data.get("release-groups", [])
            total = int(data.get("release-group-count", len(page)))
            groups.extend(g for raw in page if (g := _parse_release_group(raw)))
            offset += len(page)
            if on_page:
                result = on_page(len(groups), total)
                if inspect.isawaitable(result):
                    await result
            if not page or offset >= total:
                break
        return groups


# ------------------------------------------------------------------------ parsing


def _parse_artist(raw: dict[str, Any]) -> ArtistHit | None:
    mbid, name = raw.get("id"), raw.get("name")
    if not mbid or not name:
        return None
    try:
        score = int(raw.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    aliases = [a["name"] for a in raw.get("aliases", []) if isinstance(a, dict) and a.get("name")]
    return ArtistHit(
        mbid=mbid,
        name=name,
        sort_name=raw.get("sort-name") or name,
        score=score,
        country=raw.get("country"),
        disambiguation=raw.get("disambiguation") or "",
        type=raw.get("type"),
        aliases=aliases,
    )


def _parse_credits(raw: Any) -> list[tuple[str, str | None, str]]:
    """track["artist-credit"]: [{"name", "joinphrase", "artist": {"id", "name"}}]."""
    credits = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        artist = item.get("artist") or {}
        name = str(item.get("name") or artist.get("name") or "").strip()
        if name:
            credits.append((name, artist.get("id") or None, str(item.get("joinphrase") or "")))
    return credits


def _parse_release(raw: dict[str, Any]) -> ReleaseData | None:
    mbid = raw.get("id")
    if not mbid:
        return None
    tracks: list[TrackData] = []
    for medium in raw.get("media") or []:
        disc = int(medium.get("position") or 1)
        for t in medium.get("tracks") or []:
            title = t.get("title") or (t.get("recording") or {}).get("title")
            if not title:
                continue
            length = t.get("length")
            tracks.append(
                TrackData(
                    disc=disc,
                    position=int(t.get("position") or len(tracks) + 1),
                    number=str(t["number"])[:16] if t.get("number") else None,
                    title=title,
                    length_ms=int(length) if isinstance(length, int | float) else None,
                    recording_mbid=(t.get("recording") or {}).get("id"),
                    credits=_parse_credits(t.get("artist-credit")),
                )
            )
    return ReleaseData(
        mbid=mbid,
        date=raw.get("date") or None,
        country=raw.get("country") or None,
        status=raw.get("status") or None,
        tracks=tracks,
    )


def _parse_release_group(raw: dict[str, Any]) -> ReleaseGroupData | None:
    mbid, title = raw.get("id"), raw.get("title")
    if not mbid or not title:
        return None
    return ReleaseGroupData(
        mbid=mbid,
        title=title,
        primary_type=raw.get("primary-type") or None,
        secondary_types=[str(t) for t in raw.get("secondary-types") or []],
        first_release_date=raw.get("first-release-date") or None,
        disambiguation=raw.get("disambiguation") or "",
    )
