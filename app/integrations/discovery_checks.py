"""Connection tests of the discovery services (Last.fm, ListenBrainz, iTunes, Deezer)."""

import httpx

from app.integrations.base import CheckResult, Integration
from app.integrations.listenbrainz import ALGORITHM

# Well known public examples: Radiohead's MBID and a Deezer album id.
SAMPLE_ARTIST_MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
SAMPLE_DEEZER_ALBUM = "302127"


def _headers(integration: Integration) -> dict[str, str]:
    from app import __version__

    agent = integration.settings.mb_user_agent.strip() or f"Diskko/{__version__}"
    return {"User-Agent": agent, "Accept": "application/json"}


def _json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return None


class LastFm(Integration):
    name = "lastfm"

    def is_configured(self) -> bool:
        return bool(self.settings.lastfm_api_key.strip())

    async def check(self, client: httpx.AsyncClient) -> CheckResult:
        resp = await client.get(
            self.settings.lastfm_url,
            params={
                "method": "artist.getsimilar",
                "artist": "Cher",
                "limit": 1,
                "format": "json",
                "api_key": self.settings.lastfm_api_key.strip(),
            },
            headers=_headers(self),
        )
        data = _json(resp)
        error = data.get("error") if isinstance(data, dict) else None
        if error in (10, 26) or resp.status_code in (401, 403):
            return CheckResult(False, "auth_failed")
        if resp.status_code == 200 and error is None and isinstance(data, dict):
            return CheckResult(True, "ok")
        if error == 29:
            return CheckResult(False, "rate_limited")
        return CheckResult(False, "http_error", str(error or resp.status_code))


class ListenBrainz(Integration):
    name = "listenbrainz"

    def is_configured(self) -> bool:
        return bool(self.settings.mb_user_agent.strip())  # ListenBrainz asks for a contact

    async def check(self, client: httpx.AsyncClient) -> CheckResult:
        resp = await client.get(
            f"{self.settings.listenbrainz_url.rstrip('/')}/similar-artists/json",
            params={"artist_mbids": SAMPLE_ARTIST_MBID, "algorithm": ALGORITHM},
            headers=_headers(self),
        )
        if resp.status_code == 429:
            return CheckResult(False, "rate_limited")
        if resp.status_code != 200:
            return CheckResult(False, "http_error", str(resp.status_code))
        return (
            CheckResult(True, "ok")
            if isinstance(_json(resp), list)
            else CheckResult(False, "invalid_response")
        )


class Itunes(Integration):
    name = "itunes"

    def is_configured(self) -> bool:
        return True  # public, no key

    async def check(self, client: httpx.AsyncClient) -> CheckResult:
        resp = await client.get(
            f"{self.settings.itunes_url.rstrip('/')}/search",
            params={"term": "radiohead", "media": "music", "entity": "album", "limit": 1},
            headers=_headers(self),
        )
        if resp.status_code != 200:
            return CheckResult(False, "http_error", str(resp.status_code))
        data = _json(resp)
        ok = isinstance(data, dict) and "resultCount" in data
        return CheckResult(True, "ok") if ok else CheckResult(False, "invalid_response")


class Deezer(Integration):
    name = "deezer"

    def is_configured(self) -> bool:
        return True  # public, no key

    async def check(self, client: httpx.AsyncClient) -> CheckResult:
        resp = await client.get(
            f"{self.settings.deezer_url.rstrip('/')}/album/{SAMPLE_DEEZER_ALBUM}",
            headers=_headers(self),
        )
        if resp.status_code != 200:
            return CheckResult(False, "http_error", str(resp.status_code))
        data = _json(resp)
        if isinstance(data, dict) and data.get("error"):  # errors arrive with HTTP 200
            return CheckResult(False, "invalid_response")
        return (
            CheckResult(True, "ok")
            if isinstance(data, dict)
            else CheckResult(False, "invalid_response")
        )
