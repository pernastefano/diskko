"""Settings editable from the web UI.

Two layers: environment variables (and .env) give the defaults and the bootstrap values
(SECRET_KEY, DATA_DIR, ...); what an administrator saves in the UI is stored in the
`settings` table under `config.<name>` and wins over the environment. After a change the
effective settings are rebuilt and applied to the running app without a restart.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.integrations.providers import CAPABILITIES
from app.models import Setting
from app.services.app_settings import set_setting
from app.services.quality_profiles import load_profiles

STORAGE_PREFIX = "config."
MAX_WORKERS = 8
QUEUES = ("default", "library", "musicbrainz", "coverart", "slskd", "discovery")
SOURCES = ("filesystem", "plex")

_PATH_MAP_RULE = re.compile(r"^[^=;]+=[^=;]+$")


class FieldError(Exception):
    """A value was rejected; `code` maps to the i18n key `settings.error.<code>`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Field:
    key: str  # attribute of Settings
    # text | url | path | int | bool | choice | cron | path_map | secret | concurrency
    kind: str
    section: str  # library | integrations | jobs | albums
    group: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False
    max_length: int = 255


FIELDS: dict[str, Field] = {
    f.key: f
    for f in (
        # library
        Field("music_dir", "path", "library", "library", required=True, max_length=1024),
        Field("library_source", "choice", "library", "library", choices=SOURCES, required=True),
        Field("library_scan_cron", "cron", "library", "library"),
        # integrations
        Field("plex_url", "url", "integrations", "plex"),
        Field("plex_token", "secret", "integrations", "plex"),
        Field("plex_music_section", "text", "integrations", "plex", required=True),
        Field("library_path_map", "path_map", "integrations", "plex", max_length=1024),
        Field("slskd_url", "url", "integrations", "slskd"),
        Field("slskd_api_key", "secret", "integrations", "slskd"),
        Field("downloads_dir", "path", "integrations", "slskd", max_length=1024),
        Field("slskd_mode", "choice", "integrations", "slskd", choices=("auto", "manual")),
        Field("slskd_search_timeout", "int", "integrations", "slskd", minimum=10, maximum=300),
        Field("slskd_candidates", "int", "integrations", "slskd", minimum=1, maximum=20),
        Field("mb_user_agent", "text", "integrations", "musicbrainz", max_length=200),
        Field("mb_cache_ttl_days", "int", "integrations", "musicbrainz", minimum=1, maximum=365),
        Field("mb_refresh_days", "int", "integrations", "musicbrainz", minimum=1, maximum=365),
        Field("coverart_enabled", "bool", "integrations", "coverart"),
        Field("lastfm_api_key", "secret", "integrations", "lastfm"),
        # which service answers what (default first, then the fallbacks)
        *(
            Field(key, "providers", "integrations", "providers", choices=known, required=True)
            for key, known in CAPABILITIES.values()
        ),
        Field(
            "provider_breaker_threshold", "int", "integrations", "providers", minimum=1, maximum=100
        ),
        Field(
            "provider_breaker_cooldown_minutes",
            "int",
            "integrations",
            "providers",
            minimum=1,
            maximum=1440,
        ),
        # jobs
        Field("job_queue_concurrency", "concurrency", "jobs", "jobs"),
        Field("job_retention_routine_days", "int", "jobs", "retention", minimum=1, maximum=3650),
        Field("job_retention_success_days", "int", "jobs", "retention", minimum=1, maximum=3650),
        Field("job_retention_failed_days", "int", "jobs", "retention", minimum=1, maximum=3650),
        Field("job_retention_overrides", "retention", "jobs", "retention", max_length=500),
        # quality: the key must name an existing profile, checked where it is saved
        Field("default_quality", "text", "quality", "quality", required=True, max_length=48),
    )
}


# What the settings offer for a schedule: (id, crontab). The crontab is what gets stored, so
# LIBRARY_SCAN_CRON and values saved earlier keep working; anything else shows as "custom".
SCHEDULE_PRESETS: tuple[tuple[str, str], ...] = (
    ("never", ""),
    ("hourly", "0 * * * *"),
    ("every_6_hours", "0 */6 * * *"),
    ("every_12_hours", "0 */12 * * *"),
    ("daily", "0 2 * * *"),
    ("weekly", "0 2 * * 0"),
    ("monthly", "0 2 1 * *"),
)


def schedule_id(cron: str) -> str | None:
    """The preset a crontab expression stands for, if any."""
    normal = " ".join(cron.split())
    return next((i for i, c in SCHEDULE_PRESETS if c == normal), None)


def fields_of(section: str, group: str | None = None) -> list[Field]:
    return [
        f for f in FIELDS.values() if f.section == section and (group is None or f.group == group)
    ]


# ------------------------------------------------------------------- validation


def _text(field: Field, raw: str) -> str:
    value = raw.strip()
    if "\x00" in value or "\n" in value or "\r" in value:
        raise FieldError("invalid_characters")
    if not value and field.required:
        raise FieldError("required")
    if len(value) > field.max_length:
        raise FieldError("too_long")
    return value


def _url(field: Field, raw: str) -> str:
    value = _text(field, raw)
    if not value:
        return ""
    if any(c.isspace() for c in value):
        raise FieldError("url_invalid")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FieldError("url_invalid")
    if parts.username or parts.password:
        raise FieldError("url_credentials")  # secrets belong in their own field
    try:
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError as exc:
        raise FieldError("url_invalid") from exc
    return value.rstrip("/")


def _path(field: Field, raw: str) -> str:
    value = _text(field, raw)
    if not os.path.isabs(value):
        raise FieldError("path_not_absolute")
    return os.path.normpath(value)


def _int(field: Field, raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise FieldError("number_invalid") from exc
    if (field.minimum is not None and value < field.minimum) or (
        field.maximum is not None and value > field.maximum
    ):
        raise FieldError("number_range")
    return value


_RETENTION_ENTRY = re.compile(r"^[a-z][a-z0-9_.]{0,63}:\d{1,4}$")


def _retention(field: Field, raw: str) -> str:
    """'type:days' pairs, one per job type; each at least 1 day. Normalised to 'a:1,b:2'."""
    value = _text(field, raw.replace("\r", "").replace("\n", ","))
    entries: dict[str, int] = {}
    for part in filter(None, (p.strip().lower() for p in value.split(","))):
        if not _RETENTION_ENTRY.match(part):
            raise FieldError("retention_invalid")
        name, _, days = part.partition(":")
        if not 1 <= int(days) <= 3650:
            raise FieldError("number_range")
        entries[name] = int(days)
    return ",".join(f"{n}:{d}" for n, d in entries.items())


def _cron(field: Field, raw: str) -> str:
    value = _text(field, raw)
    if value:
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise FieldError("cron_invalid") from exc
    return value


def _path_map(field: Field, raw: str) -> str:
    value = _text(field, raw.replace("\r", "").replace("\n", ";"))
    for rule in filter(None, (r.strip() for r in value.split(";"))):
        if not _PATH_MAP_RULE.match(rule):
            raise FieldError("path_map_invalid")
    return value


def parse_concurrency(raw: Mapping[str, str]) -> str:
    """{'default': '2', ...} -> 'default:2,...'. MusicBrainz is always 1."""
    parts = []
    for queue in QUEUES:
        value = raw.get(queue, "").strip()
        if queue == "musicbrainz":
            parts.append("musicbrainz:1")
            continue
        if not value:
            continue
        try:
            count = int(value)
        except ValueError as exc:
            raise FieldError("number_invalid") from exc
        if not 1 <= count <= MAX_WORKERS:
            raise FieldError("number_range")
        parts.append(f"{queue}:{count}")
    return ",".join(parts)


def parse_providers(field: Field, items: list[str]) -> str:
    """Ordered provider names -> 'a,b'. Blanks are skipped, the first one is required."""
    names = [i.strip().lower() for i in items if i.strip()]
    if not names:
        raise FieldError("required")
    if any(n not in field.choices for n in names):
        raise FieldError("choice_invalid")
    if len(set(names)) != len(names):
        raise FieldError("providers_duplicate")
    return ",".join(names)


def parse_value(field: Field, raw: Any) -> Any:
    """Validate one submitted value and return what gets stored. Raises FieldError."""
    if field.kind == "bool":
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    if field.kind == "choice":
        value = str(raw).strip()
        if value not in field.choices:
            raise FieldError("choice_invalid")
        return value
    if field.kind == "providers":
        return parse_providers(field, str(raw).split(","))
    if field.kind == "secret":
        value = str(raw).strip()
        if not value:
            raise FieldError("required")
        if len(value) > 512 or any(c.isspace() for c in value):
            raise FieldError("secret_invalid")
        return value
    text = str(raw)
    return {
        "text": _text,
        "url": _url,
        "path": _path,
        "int": _int,
        "cron": _cron,
        "retention": _retention,
        "path_map": _path_map,
    }[field.kind](field, text)


# --------------------------------------------------------------- storage layer


def read_overrides(db: Session) -> dict[str, Any]:
    """Saved values by Settings attribute name (unknown or obsolete keys are ignored)."""
    result: dict[str, Any] = {}
    rows = db.scalars(
        select(Setting).where(Setting.scope == "global", Setting.key.like(f"{STORAGE_PREFIX}%"))
    )
    import json

    for row in rows:
        key = row.key.removeprefix(STORAGE_PREFIX)
        if key in FIELDS:
            result[key] = json.loads(row.value_json)
    return result


def effective_settings(base: Settings, overrides: Mapping[str, Any]) -> Settings:
    """The environment settings with the saved values applied on top."""
    update = dict(overrides)
    if "music_dir" in update:
        update["music_dir"] = Path(update["music_dir"])
    return base.model_copy(update=update)


def save_values(
    db: Session,
    fields: list[Field],
    form: Mapping[str, Any],
    *,
    reset: set[str] = frozenset(),
) -> dict[str, str]:
    """Validate and store the submitted fields. Returns {key: error code}; nothing is saved
    unless every field is valid. The caller commits.

    A blank secret keeps the stored value. Keys in `reset` go back to the environment default.
    """
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}
    for field in fields:
        if field.key in reset:
            continue
        if field.kind == "concurrency":
            raw = {q: str(form.get(f"queue_{q}", "")) for q in QUEUES}
            try:
                values[field.key] = parse_concurrency(raw)
            except FieldError as exc:
                errors[field.key] = exc.code
            continue
        if field.kind == "providers":
            slots = [form.get(f"{field.key}_{i}") for i in range(1, len(field.choices) + 1)]
            if all(slot is None for slot in slots):
                continue
            try:
                values[field.key] = parse_providers(field, [str(s or "") for s in slots])
            except FieldError as exc:
                errors[field.key] = exc.code
            continue
        raw = form.get(field.key)
        if field.kind == "bool":
            values[field.key] = parse_value(field, raw if raw is not None else "")
            continue
        if raw is None:
            continue
        if field.kind == "secret" and not str(raw).strip():
            continue  # blank: keep what is stored
        try:
            values[field.key] = parse_value(field, raw)
        except FieldError as exc:
            errors[field.key] = exc.code
    if errors:
        return errors
    for key, value in values.items():
        set_setting(db, STORAGE_PREFIX + key, value)
    clear_overrides(db, [k for k in reset if k in FIELDS])
    return {}


def clear_overrides(db: Session, keys: list[str]) -> None:
    if keys:
        db.execute(
            delete(Setting).where(
                Setting.scope == "global", Setting.key.in_([STORAGE_PREFIX + k for k in keys])
            )
        )


def overridden_keys(db: Session) -> set[str]:
    return set(read_overrides(db))


# ------------------------------------------------------------ apply to the app


def apply_runtime_config(app) -> Settings:
    """Rebuild the effective settings from the database and push them into the running app."""
    with app.state.session_factory() as db:
        overrides = read_overrides(db)
        app.state.quality_profiles = load_profiles(db)
    effective = effective_settings(app.state.base_settings, overrides)
    app.state.settings = effective
    if getattr(app.state, "worker", None) is not None:
        app.state.worker.update_settings(effective)
    if getattr(app.state, "scheduler", None) is not None:
        app.state.scheduler.reschedule(effective)
    return effective
