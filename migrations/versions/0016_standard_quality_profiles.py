"""the standard quality profiles: plain names, Hi-Res from 96 kHz, Master from 192 kHz

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-08 09:00:00.000000

Only profiles that still have the values the app created are updated: one that was edited in the
settings is left alone. Tracks and albums are classified again where the new thresholds change
the result (24-bit files between 88.2 and 96 kHz, and between 176.4 and 192 kHz).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# key: (old name, old min sample rate, new name, new min sample rate)
_CHANGES = {
    "any": ("Qualsiasi / bassa", None, "Qualsiasi", None),
    "flac_standard": ("FLAC Standard (16/44.1)", 44100, "FLAC Standard", 44100),
    "flac_hires": ("FLAC Hi-Res (24/96)", 88200, "FLAC Hi-Res", 96000),
    "flac_master": ("FLAC Master (24/192)", 176400, "FLAC Master", 192000),
}


def _update(connection, to_new: bool) -> set[str]:
    """Rename and re-threshold the profiles that hold the old (or, going back, the new) values."""
    changed: set[str] = set()
    for key, (old_name, old_rate, new_name, new_rate) in _CHANGES.items():
        src = (old_name, old_rate) if to_new else (new_name, new_rate)
        dst = (new_name, new_rate) if to_new else (old_name, old_rate)
        rate_is_same = (
            "min_sample_rate IS NULL" if src[1] is None else "min_sample_rate = :src_rate"
        )
        result = connection.execute(
            sa.text(
                "UPDATE quality_profiles SET name = :dst_name, min_sample_rate = :dst_rate "
                f"WHERE key = :key AND name = :src_name AND {rate_is_same}"
            ),
            {
                "key": key,
                "src_name": src[0],
                "src_rate": src[1],
                "dst_name": dst[0],
                "dst_rate": dst[1],
            },
        )
        if result.rowcount:
            changed.add(key)
    return changed


def _reclassify(connection, changed: set[str]) -> None:
    """Move the tracks that fall on the other side of a moved threshold (Master files below
    192 kHz become Hi-Res, Hi-Res files below 96 kHz become Standard), then give every album the
    quality of its weakest track again."""
    steps = []
    if "flac_master" in changed:
        steps.append(("flac_master", 192000, "flac_hires"))
    if "flac_hires" in changed:
        steps.append(("flac_hires", 96000, "flac_standard"))
    present = {r[0] for r in connection.execute(sa.text("SELECT key FROM quality_profiles"))}
    for tier, floor, lower in steps:
        if lower not in present:
            continue
        connection.execute(
            sa.text(
                "UPDATE library_tracks SET quality_tier = :lower "
                "WHERE quality_tier = :tier AND COALESCE(sample_rate, 44100) < :floor"
            ),
            {"tier": tier, "floor": floor, "lower": lower},
        )
    connection.execute(
        sa.text(
            "UPDATE library_albums SET quality_tier = COALESCE("
            "(SELECT p.key FROM library_tracks t JOIN quality_profiles p "
            "ON p.key = t.quality_tier WHERE t.album_id = library_albums.id "
            "ORDER BY p.rank ASC LIMIT 1), quality_tier)"
        )
    )


def upgrade() -> None:
    connection = op.get_bind()
    changed = _update(connection, to_new=True)
    if changed & {"flac_hires", "flac_master"}:
        _reclassify(connection, changed)


def downgrade() -> None:
    # The names and thresholds go back; tracks are not moved up again (it would need their raw
    # sample rates and bit depths, and the next scan or "recompute" does it).
    _update(op.get_bind(), to_new=False)
