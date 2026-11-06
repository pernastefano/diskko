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
