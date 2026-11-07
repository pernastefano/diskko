"""split lossless quality into FLAC Standard / Studio / Hi-Res / Master

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RANK = (
    "CASE quality_tier WHEN 'any' THEN 0 WHEN 'mp3_192' THEN 1 WHEN 'mp3_256' THEN 2 "
    "WHEN 'mp3_320' THEN 3 WHEN 'flac_standard' THEN 4 WHEN 'flac_studio' THEN 5 "
    "WHEN 'flac_hires' THEN 6 WHEN 'flac_master' THEN 7 ELSE 0 END"
)


def upgrade() -> None:
    # Lossless tracks are re-classified from their stored sample rate and bit depth
    # (unknown values are read as CD quality), then each album takes its weakest track.
    op.execute(
        """
        UPDATE library_tracks SET quality_tier = CASE
            WHEN COALESCE(sample_rate, 44100) >= 176400 THEN 'flac_master'
            WHEN COALESCE(sample_rate, 44100) >= 88200 THEN 'flac_hires'
            WHEN COALESCE(sample_rate, 44100) >= 48000 OR COALESCE(bit_depth, 16) >= 24
                THEN 'flac_studio'
            ELSE 'flac_standard' END
        WHERE lossless = 1
        """
    )
    op.execute(
        f"""
        UPDATE library_albums SET quality_tier = (
            SELECT t.quality_tier FROM library_tracks t WHERE t.album_id = library_albums.id
            ORDER BY {_RANK.replace("quality_tier", "t.quality_tier")} LIMIT 1)
        WHERE EXISTS (SELECT 1 FROM library_tracks t WHERE t.album_id = library_albums.id)
        """
    )


def downgrade() -> None:
    op.execute(
        "UPDATE library_tracks SET quality_tier = CASE "
        "WHEN quality_tier = 'flac_standard' THEN 'lossless' "
        "WHEN quality_tier IN ('flac_studio', 'flac_hires', 'flac_master') THEN 'lossless_24' "
        "ELSE quality_tier END"
    )
    op.execute(
        "UPDATE library_albums SET quality_tier = CASE "
        "WHEN quality_tier = 'flac_standard' THEN 'lossless' "
        "WHEN quality_tier IN ('flac_studio', 'flac_hires', 'flac_master') THEN 'lossless_24' "
        "ELSE quality_tier END"
    )


__all__ = ["sa"]
