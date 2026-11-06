from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables (and .env in dev)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "change-me"
    base_url: str = "http://localhost:8080"
    tz: str = "UTC"

    # All paths come from configuration, never hardcoded.
    config_dir: Path = Path("./config")
    music_dir: Path = Path("./music-test")
    downloads_dir: Path = Path("./downloads-test")


@lru_cache
def get_settings() -> Settings:
    return Settings()
