"""Key/value settings stored in the `settings` table (global or per user)."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting


def _query(key: str, user_id: int | None):
    stmt = select(Setting).where(Setting.key == key)
    if user_id is None:
        return stmt.where(Setting.scope == "global", Setting.user_id.is_(None))
    return stmt.where(Setting.scope == "user", Setting.user_id == user_id)


def get_setting(db: Session, key: str, default: Any = None, user_id: int | None = None) -> Any:
    row = db.scalar(_query(key, user_id))
    return default if row is None else json.loads(row.value_json)


def set_setting(db: Session, key: str, value: Any, user_id: int | None = None) -> None:
    """Upsert a setting. The caller commits."""
    row = db.scalar(_query(key, user_id))
    payload = json.dumps(value)
    if row is None:
        db.add(
            Setting(
                key=key,
                value_json=payload,
                scope="global" if user_id is None else "user",
                user_id=user_id,
            )
        )
    else:
        row.value_json = payload
    db.flush()
