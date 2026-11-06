"""Registry of integrations and persistence of their last check result."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.integrations.base import Integration
from app.integrations.musicbrainz import MusicBrainz
from app.integrations.plex import Plex
from app.integrations.slskd import Slskd
from app.models import ACTIVE_STATUSES, Job
from app.services.app_settings import get_setting

INTEGRATION_NAMES = ("plex", "slskd", "musicbrainz")
_CLASSES: dict[str, type[Integration]] = {"plex": Plex, "slskd": Slskd, "musicbrainz": MusicBrainz}


def build_integration(name: str, settings: Settings) -> Integration | None:
    cls = _CLASSES.get(name)
    return cls(settings) if cls else None


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
