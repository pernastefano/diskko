"""Registry of integrations and persistence of their last check result."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.integrations.base import CheckResult, Integration
from app.integrations.discovery_checks import Deezer, Itunes, LastFm, ListenBrainz
from app.integrations.musicbrainz import MusicBrainz
from app.integrations.plex import Plex
from app.integrations.slskd import Slskd
from app.models import ACTIVE_STATUSES, Job, utcnow
from app.services.app_settings import get_setting, set_setting

INTEGRATION_NAMES = ("plex", "slskd", "musicbrainz", "lastfm", "listenbrainz", "itunes", "deezer")
_CLASSES: dict[str, type[Integration]] = {
    "plex": Plex,
    "slskd": Slskd,
    "musicbrainz": MusicBrainz,
    "lastfm": LastFm,
    "listenbrainz": ListenBrainz,
    "itunes": Itunes,
    "deezer": Deezer,
}


def build_integration(name: str, settings: Settings) -> Integration | None:
    cls = _CLASSES.get(name)
    return cls(settings) if cls else None


def record_result(db: Session, name: str, result: CheckResult) -> None:
    """Remember the outcome of a check (the caller commits)."""
    record = result.to_dict() | {"checked_at": utcnow().isoformat(timespec="seconds")}
    set_setting(db, f"integration_status.{name}", record)


def check_dedupe_key(name: str) -> str:
    return f"integration_check:{name}"


def last_result(db: Session, name: str) -> dict | None:
    return get_setting(db, f"integration_status.{name}")


def check_in_progress(db: Session, name: str) -> bool:
    return (
        db.scalar(
            select(Job.id).where(
                Job.dedupe_key == check_dedupe_key(name), Job.status.in_(ACTIVE_STATUSES)
            )
        )
        is not None
    )


# ----------------------------------------------------------------------- overall health

HEALTH_OK = "ok"  # every configured service answers
HEALTH_PARTIAL = "partial"  # some do, some do not
HEALTH_DOWN = "down"  # none does
HEALTH_UNKNOWN = "unknown"  # no recent result yet (just started, or checks are off)


@dataclass(frozen=True)
class Health:
    state: str
    connected: int
    configured: int


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def integration_health(
    db: Session, settings: Settings, *, now: Callable[[], datetime] = utcnow
) -> Health:
    """The state behind the status icon, from the last results of the CONFIGURED services.

    A result counts only while it is recent (three check intervals): if the checks stop, the
    icon goes back to neutral instead of showing an old green. A configured service that has
    not been checked yet counts as not connected.
    """
    configured = [
        n
        for n in INTEGRATION_NAMES
        if (integration := build_integration(n, settings)) and integration.is_configured()
    ]
    minutes = settings.status_check_minutes
    max_age = timedelta(minutes=3 * minutes) if minutes > 0 else None
    connected = checked = 0
    for name in configured:
        result = last_result(db, name)
        when = _parse(result.get("checked_at")) if result else None
        if not result or when is None or (max_age and now() - when > max_age):
            continue
        checked += 1
        connected += bool(result.get("ok"))
    if not checked:
        state = HEALTH_UNKNOWN
    elif connected == len(configured):
        state = HEALTH_OK
    elif connected == 0:
        state = HEALTH_DOWN
    else:
        state = HEALTH_PARTIAL
    return Health(state, connected, len(configured))
