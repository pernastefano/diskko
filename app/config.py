from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.quality import DEFAULT_QUALITY

DEFAULT_SECRET_KEY = "change-me"


class Settings(BaseSettings):
    """Application settings, read from environment variables (and .env in dev)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    secret_key: str = DEFAULT_SECRET_KEY
    base_url: str = "http://localhost:8080"
    tz: str = "UTC"

    # All paths come from configuration, never hardcoded.
    data_dir: Path = Path("./data")
    music_dir: Path = Path("./music-test")
    downloads_dir: Path = Path("./downloads-test")

    # Sessions / auth
    session_cookie_name: str = "diskko_session"
    session_ttl_hours: int = 24 * 14
    min_password_length: int = 8

    # Job engine
    jobs_enabled: bool = True
    job_queue_concurrency: str = "default:2,musicbrainz:1,slskd:6,library:1,coverart:2,discovery:2"
    job_poll_interval: float = 1.0
    # How many days finished jobs and their logs are kept (at least 1). The jobs that run all day
    # (service checks, the cleanup) go quickly; failures stay longer, they explain what went wrong.
    job_retention_routine_days: int = 1
    job_retention_success_days: int = 14
    job_retention_failed_days: int = 60
    job_retention_overrides: str = ""  # "scan_library:90,fetch_covers:7": days per job type
    # How often the connection to every configured service is checked (0 turns it off). Five
    # minutes keeps the status icon fresh and costs a handful of tiny requests.
    status_check_minutes: int = 5
    job_log_max_lines: int = 5000

    # Library
    library_source: str = "filesystem"  # filesystem | plex
    library_scan_cron: str = "0 2 * * *"  # crontab; empty disables the nightly scan
    library_max_removal_pct: int = 50  # abort a scan that would drop more than this % of tracks
    plex_music_section: str = "Music"
    library_path_map: str = ""  # "plex_prefix=local_prefix;..." (Plex source only)

    # Integrations (all optional; connection tests are jobs, see app/integrations)
    plex_url: str = ""
    plex_token: str = ""
    slskd_url: str = ""
    slskd_api_key: str = ""
    # Searching and downloading through SLSKD
    slskd_search_timeout: int = 30  # seconds without a new answer before a search ends
    slskd_candidates: int = 5  # candidates kept (and shown for a manual choice)
    slskd_mode: str = "auto"  # auto: download the best candidate; manual: the user picks one
    slskd_poll_seconds: float = 5.0  # how often a running download is looked at
    slskd_stall_minutes: float = 10  # no byte received for this long: try the next candidate
    slskd_queue_minutes: float = 120  # waiting this long in the remote queue: try the next one
    slskd_max_hours: float = 12  # one candidate may take this long at most
    slskd_file_retries: int = 2  # a failed file is asked again this many times
    mb_user_agent: str = ""
    musicbrainz_url: str = "https://musicbrainz.org/ws/2"
    coverart_url: str = "https://coverartarchive.org"
    # Discovery (similar artists, previews). Every one of them is public and read only.
    lastfm_url: str = "https://ws.audioscrobbler.com/2.0/"
    listenbrainz_url: str = "https://labs.api.listenbrainz.org"
    itunes_url: str = "https://itunes.apple.com"
    wikidata_url: str = "https://www.wikidata.org/w/api.php"
    wikipedia_url: str = "https://{lang}.wikipedia.org"  # {lang} is replaced: en, it
    deezer_url: str = "https://api.deezer.com"
    preview_country: str = "US"  # iTunes catalogue to search (two-letter country code)
    coverart_min_interval: float = 0.5  # seconds between Cover Art Archive requests
    mb_cache_ttl_days: int = 7
    mb_min_interval: float = 1.1  # seconds between requests (MusicBrainz allows 1 per second)
    mb_max_retries: int = 3
    mb_refresh_days: int = 30  # re-sync artists whose metadata is older than this
    coverart_enabled: bool = True
    # Providers per capability: the first is the default, the others are fallbacks, in order
    providers_covers: str = "coverartarchive,deezer"
    providers_previews: str = "itunes,deezer"
    providers_similar: str = "lastfm,listenbrainz"
    providers_toptracks: str = "deezer,lastfm"  # Last.fm lists, iTunes adds the previews
    providers_bio: str = "wikipedia,lastfm"
    providers_metadata: str = "musicbrainz,deezer"  # MusicBrainz first; Deezer fills the gaps
    provider_breaker_threshold: int = 5  # consecutive failures that pause a service
    provider_breaker_cooldown_minutes: int = 10
    lastfm_api_key: str = ""

    # Downloads: key of the quality profile a request must reach unless it says otherwise
    default_quality: str = DEFAULT_QUALITY

    @property
    def retention_overrides(self) -> dict[str, int]:
        """Parse 'type:days,type:days'. Invalid entries are ignored; the least is 1 day."""
        result: dict[str, int] = {}
        for part in self.job_retention_overrides.split(","):
            name, _, days = part.strip().partition(":")
            if name and days.strip().isdigit() and int(days) >= 1:
                result[name.strip()] = int(days)
        return result

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
        values = [self.secret_key, self.plex_token, self.slskd_api_key, self.lastfm_api_key]
        return [v for v in values if v and len(v) >= 4]

    @property
    def database_url(self) -> str:
        return f"sqlite:///{(self.data_dir / 'app.db').resolve()}"

    @property
    def cookie_secure(self) -> bool:
        return self.base_url.lower().startswith("https://")

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production" or self.cookie_secure


@lru_cache
def get_settings() -> Settings:
    return Settings()
