"""artist profile: biography, top tracks and the artist's id at Deezer

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-07 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    have = _columns("artists")
    with op.batch_alter_table("artists", schema=None) as batch_op:
        if "deezer_artist_id" not in have:
            batch_op.add_column(sa.Column("deezer_artist_id", sa.String(length=32), nullable=True))
        if "profile_checked_at" not in have:
            batch_op.add_column(sa.Column("profile_checked_at", sa.DateTime(), nullable=True))
        if "top_checked_at" not in have:
            batch_op.add_column(sa.Column("top_checked_at", sa.DateTime(), nullable=True))

    tables = _tables()
    if "artist_bios" not in tables:
        op.create_table(
            "artist_bios",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("artist_id", sa.Integer(), nullable=False),
            sa.Column("lang", sa.String(length=8), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=True),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("checked_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["artist_id"],
                ["artists.id"],
                name=op.f("fk_artist_bios_artist_id_artists"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_artist_bios")),
            sa.UniqueConstraint("artist_id", "lang", name=op.f("uq_artist_bios_artist_id")),
        )
        with op.batch_alter_table("artist_bios", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_artist_bios_artist_id"), ["artist_id"])
    if "artist_top_tracks" not in tables:
        op.create_table(
            "artist_top_tracks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("artist_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("album_title", sa.String(length=512), nullable=True),
            sa.Column("duration_s", sa.Integer(), nullable=True),
            sa.Column("provider", sa.String(length=16), nullable=False),
            sa.Column("track_url", sa.Text(), nullable=True),
            sa.Column("preview_url", sa.Text(), nullable=True),
            sa.Column("preview_provider", sa.String(length=16), nullable=True),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["artist_id"],
                ["artists.id"],
                name=op.f("fk_artist_top_tracks_artist_id_artists"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_artist_top_tracks")),
            sa.UniqueConstraint(
                "artist_id", "position", name=op.f("uq_artist_top_tracks_artist_id")
            ),
        )
        with op.batch_alter_table("artist_top_tracks", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_artist_top_tracks_artist_id"), ["artist_id"])


def downgrade() -> None:
    op.drop_table("artist_top_tracks")
    op.drop_table("artist_bios")
    with op.batch_alter_table("artists", schema=None) as batch_op:
        batch_op.drop_column("top_checked_at")
        batch_op.drop_column("profile_checked_at")
        batch_op.drop_column("deezer_artist_id")
