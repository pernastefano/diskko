import httpx

from app.integrations.base import CheckResult, Integration


class Plex(Integration):
    name = "plex"

    def is_configured(self) -> bool:
        return bool(self.settings.plex_url.strip() and self.settings.plex_token.strip())

    async def check(self, client: httpx.AsyncClient) -> CheckResult:
        base = self.settings.plex_url.rstrip("/")
        headers = {"Accept": "application/json", "X-Plex-Token": self.settings.plex_token}
        # 1) the server answers (GET /identity does not need a token)
        ident = await client.get(f"{base}/identity", headers=headers)
        if ident.status_code != 200:
            return CheckResult(False, "http_error", str(ident.status_code))
        # 2) the token is accepted
        sections = await client.get(f"{base}/library/sections", headers=headers)
        if sections.status_code in (401, 403):
            return CheckResult(False, "auth_failed")
        if sections.status_code != 200:
            return CheckResult(False, "http_error", str(sections.status_code))
        return CheckResult(True, "ok", _version(ident))


def _version(resp: httpx.Response) -> str:
    try:
        version = resp.json()["MediaContainer"]["version"]
    except (ValueError, KeyError, TypeError):
        return ""
    return version if isinstance(version, str) else ""
