from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET_KEY = "change-me"


class Settings(BaseSettings):
    """Application settings, read from environment variables (and .env in dev)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    secret_key: str = DEFAULT_SECRET_KEY
    base_url: str = "http://localhost:8080"
    tz: str = "UTC"

    # All paths come from configuration, never hardcoded.
    config_dir: Path = Path("./config")
    music_dir: Path = Path("./music-test")
    downloads_dir: Path = Path("./downloads-test")

    # Sessions / auth
    session_cookie_name: str = "diskko_session"
    session_ttl_hours: int = 24 * 14
    min_password_length: int = 10

    # Job engine
    jobs_enabled: bool = True
    job_queue_concurrency: str = "default:2,musicbrainz:1,slskd:2"
    job_poll_interval: float = 1.0
    job_retention_days: int = 30
    job_log_max_lines: int = 5000

    # Integrations (all optional; connection tests are jobs, see app/integrations)
    plex_url: str = ""
    plex_token: str = ""
    slskd_url: str = ""
    slskd_api_key: str = ""
    mb_user_agent: str = ""
    musicbrainz_url: str = "https://musicbrainz.org/ws/2"

    @property
    def queue_concurrency(self) -> dict[str, int]:
        """Parse 'name:n,name:n'. Invalid entries are ignored; MusicBrainz is capped at 1."""
        result: dict[str, int] = {}
        for part in self.job_queue_concurrency.split(","):
            name, _, count = part.strip().partition(":")
            if name and count.strip().isdigit() and int(count) >= 1:
                result[name.strip()] = int(count)
        result["musicbrainz"] = 1  # MusicBrainz allows 1 request/second: never parallel
        return result

    @property
    def secret_values(self) -> list[str]:
        """Values that must never appear in logs or error messages."""
        values = [self.secret_key, self.plex_token, self.slskd_api_key]
        return [v for v in values if v and len(v) >= 4]

    @property
    def database_url(self) -> str:
        return f"sqlite:///{(self.config_dir / 'app.db').resolve()}"

    @property
    def cookie_secure(self) -> bool:
        return self.base_url.lower().startswith("https://")

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production" or self.cookie_secure


@lru_cache
def get_settings() -> Settings:
    return Settings()
