"""configurable quality profiles; tracks remember whether they are LAME V0

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (key, name, kind, rank, min_bit_depth, min_sample_rate, min_bitrate, accept_v0, color)
_SEED = (
    ("any", "Qualsiasi / bassa", "any", 1, None, None, None, 0, "red"),
    ("mp3_192", "MP3 ≥ 192", "lossy", 2, None, None, 192, 0, "amber"),
    ("mp3_256", "MP3 ≥ 256", "lossy", 3, None, None, 256, 0, "sky"),
    ("mp3_320", "MP3 320 / V0", "lossy", 4, None, None, 320, 1, "blue"),
    ("flac_standard", "FLAC Standard (16/44.1)", "lossless", 5, 16, 44100, None, 0, "green"),
    ("flac_hires", "FLAC Hi-Res (24/96)", "lossless", 6, 24, 88200, None, 0, "purple"),
    ("flac_master", "FLAC Master (24/192)", "lossless", 7, 24, 176400, None, 0, "fuchsia"),
)

_RANK = (
    "CASE {col} WHEN 'any' THEN 1 WHEN 'mp3_192' THEN 2 WHEN 'mp3_256' THEN 3 "
    "WHEN 'mp3_320' THEN 4 WHEN 'flac_standard' THEN 5 WHEN 'flac_hires' THEN 6 "
    "WHEN 'flac_master' THEN 7 ELSE 1 END"
)


def upgrade() -> None:
    table = op.create_table(
        "quality_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("min_bit_depth", sa.Integer(), nullable=True),
        sa.Column("min_sample_rate", sa.Integer(), nullable=True),
        sa.Column("min_bitrate", sa.Integer(), nullable=True),
        sa.Column("accept_v0", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("key"),
    )
    cols = ("key", "name", "kind", "rank", "min_bit_depth", "min_sample_rate")
    cols += ("min_bitrate", "accept_v0", "color")
    op.bulk_insert(table, [dict(zip(cols, row, strict=True)) for row in _SEED])

    with op.batch_alter_table("library_tracks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("v0", sa.Boolean(), server_default=sa.text("0"), nullable=False)
        )
        batch_op.alter_column(
            "quality_tier",
            existing_type=sa.String(length=16),
            type_=sa.String(length=48),
            existing_nullable=False,
        )
    with op.batch_alter_table("library_albums", schema=None) as batch_op:
        batch_op.alter_column(
            "quality_tier",
            existing_type=sa.String(length=16),
            type_=sa.String(length=48),
            existing_nullable=False,
        )

    # The old MP3 320 tier also held V0 files below 320 kbps: remember them as V0.
    op.execute(
        "UPDATE library_tracks SET v0 = 1 "
        "WHERE lossless = 0 AND quality_tier = 'mp3_320' AND COALESCE(bitrate, 0) < 320"
    )
    # Every track is classified again from its raw fields (whatever tier it had before), as the
    # best profile it meets; unknown bit depth / rate are read as 16-bit / 44.1 kHz.
    op.execute(
        """
        UPDATE library_tracks SET quality_tier = CASE
            WHEN lossless = 1 AND COALESCE(bit_depth, 16) >= 24
                 AND COALESCE(sample_rate, 44100) >= 176400 THEN 'flac_master'
            WHEN lossless = 1 AND COALESCE(bit_depth, 16) >= 24
                 AND COALESCE(sample_rate, 44100) >= 88200 THEN 'flac_hires'
            WHEN lossless = 1 AND COALESCE(bit_depth, 16) >= 16
                 AND COALESCE(sample_rate, 44100) >= 44100 THEN 'flac_standard'
            WHEN lossless = 1 OR v0 = 1 OR COALESCE(bitrate, 0) >= 320 THEN 'mp3_320'
            WHEN COALESCE(bitrate, 0) >= 256 THEN 'mp3_256'
            WHEN COALESCE(bitrate, 0) >= 192 THEN 'mp3_192'
            ELSE 'any' END
        """
    )
    # An album is as good as its weakest track.
    op.execute(
        f"""
        UPDATE library_albums SET quality_tier = (
            SELECT t.quality_tier FROM library_tracks t WHERE t.album_id = library_albums.id
            ORDER BY {_RANK.format(col="t.quality_tier")} LIMIT 1)
        WHERE EXISTS (SELECT 1 FROM library_tracks t WHERE t.album_id = library_albums.id)
        """
    )
    # Albums without tracks keep a known tier; anything unknown becomes the lowest one.
    op.execute(
        "UPDATE library_albums SET quality_tier = CASE "
        "WHEN quality_tier IN ('lossless', 'lossless_24', 'flac_studio') THEN 'flac_standard' "
        "WHEN quality_tier NOT IN ('any', 'mp3_192', 'mp3_256', 'mp3_320', 'flac_standard', "
        "'flac_hires', 'flac_master') THEN 'any' ELSE quality_tier END"
    )
    # Settings that named a tier that no longer exists fall back to the default.
    op.execute(
        "UPDATE settings SET value_json = '\"flac_standard\"' WHERE key = 'config.default_quality' "
        "AND value_json NOT IN ('\"any\"', '\"mp3_192\"', '\"mp3_256\"', '\"mp3_320\"', "
        "'\"flac_standard\"', '\"flac_hires\"', '\"flac_master\"')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE library_tracks SET quality_tier = 'flac_standard' WHERE quality_tier NOT IN "
        "('any', 'mp3_192', 'mp3_256', 'mp3_320', 'flac_standard', 'flac_hires', 'flac_master')"
    )
    with op.batch_alter_table("library_tracks", schema=None) as batch_op:
        batch_op.drop_column("v0")
        batch_op.alter_column(
            "quality_tier",
            existing_type=sa.String(length=48),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
    with op.batch_alter_table("library_albums", schema=None) as batch_op:
        batch_op.alter_column(
            "quality_tier",
            existing_type=sa.String(length=48),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
    op.drop_table("quality_profiles")
