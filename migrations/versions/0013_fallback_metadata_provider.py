"""fallback metadata provider: artists.provider*, release_groups.source

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-07 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    artist_columns = _columns("artists")
    with op.batch_alter_table("artists", schema=None) as batch_op:
        if "provider" not in artist_columns:
            batch_op.add_column(sa.Column("provider", sa.String(length=16), nullable=True))
        if "provider_artist_id" not in artist_columns:
            batch_op.add_column(
                sa.Column("provider_artist_id", sa.String(length=64), nullable=True)
            )
    if "source" not in _columns("release_groups"):
        with op.batch_alter_table("release_groups", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "source",
                    sa.String(length=16),
                    server_default=sa.text("'musicbrainz'"),
                    nullable=False,
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("release_groups", schema=None) as batch_op:
        batch_op.drop_column("source")
    with op.batch_alter_table("artists", schema=None) as batch_op:
        batch_op.drop_column("provider_artist_id")
        batch_op.drop_column("provider")
