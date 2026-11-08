"""Logging: readable text or one JSON object per line, with secrets scrubbed from every record."""

import json
import logging
import re
import sys
from datetime import UTC, datetime

from app.config import Settings

_SECRET_FIELDS = ("secret_key", "plex_token", "slskd_api_key", "lastfm_api_key")
# `token=abc`, `api_key: abc`, `X-Plex-Token=abc` in a URL or a message
_PAIR = re.compile(r"(?i)((?:token|api[_-]?key|secret|password)[\"']?\s*[=:]\s*[\"']?)[^\s&\"',;]+")
_MIN_SECRET = 6  # shorter values would blank out ordinary words


class RedactFilter(logging.Filter):
    """Replaces the values of the configured secrets and `token=...`-like pairs by ***."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if len(s) >= _MIN_SECRET}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return _PAIR.sub(r"\1***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.scrub(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text or record.exc_info:
            data["exception"] = record.exc_text or self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    """Set up the root logger (and make uvicorn's loggers follow it). Safe to call again."""
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format.strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter([str(getattr(settings, f, "") or "") for f in _SECRET_FIELDS]))
    handler._diskko = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "_diskko", False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.strip().upper(), logging.INFO))
    # chatty libraries whose INFO lines carry full URLs (query strings can hold tokens)
    for name in ("httpx", "httpcore", "urllib3", "apscheduler", "alembic"):
        logging.getLogger(name).setLevel(max(root.level, logging.WARNING))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
