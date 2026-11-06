import httpx

from app.integrations.base import CheckResult, Integration


class Slskd(Integration):
    name = "slskd"

    def is_configured(self) -> bool:
        return bool(self.settings.slskd_url.strip() and self.settings.slskd_api_key.strip())

    async def check(self, client: httpx.AsyncClient) -> CheckResult:
        # GET /api/v0/application is the application-state endpoint of slskd (it requires
        # authentication); the API key goes in the X-API-Key header.
        base = self.settings.slskd_url.rstrip("/")
        resp = await client.get(
            f"{base}/api/v0/application",
            headers={"X-API-Key": self.settings.slskd_api_key, "Accept": "application/json"},
        )
        if resp.status_code in (401, 403):
            return CheckResult(False, "auth_failed")
        if resp.status_code != 200:
            return CheckResult(False, "http_error", str(resp.status_code))
        return CheckResult(True, "ok", _version(resp))


def _version(resp: httpx.Response) -> str:
    """The payload shape is not documented: read the version defensively."""
    try:
        version = resp.json()["version"]
    except (ValueError, KeyError, TypeError):
        return ""
    if isinstance(version, dict):
        version = version.get("current")
    return version if isinstance(version, str) else ""
