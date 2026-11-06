import httpx

from app.integrations.base import CheckResult, Integration

# Release group used as an example in the official MusicBrainz API documentation.
SAMPLE_RELEASE_GROUP = "3bd76d40-7f0e-36b7-9348-91a33afee20e"


class MusicBrainz(Integration):
    name = "musicbrainz"

    def is_configured(self) -> bool:
        # MusicBrainz requires a User-Agent that identifies the app and a contact.
        return bool(self.settings.mb_user_agent.strip())

    async def check(self, client: httpx.AsyncClient) -> CheckResult:
        url = f"{self.settings.musicbrainz_url.rstrip('/')}/release-group/{SAMPLE_RELEASE_GROUP}"
        resp = await client.get(
            url,
            params={"fmt": "json"},
            headers={"User-Agent": self.settings.mb_user_agent, "Accept": "application/json"},
        )
        if resp.status_code == 200:
            return CheckResult(True, "ok")
        if resp.status_code == 503:  # MusicBrainz answers 503 when throttling
            return CheckResult(False, "rate_limited")
        return CheckResult(False, "http_error", str(resp.status_code))
