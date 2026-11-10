"""a release of a group chosen by hand stays the one whose tracks are kept

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-10 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("tracklist_fetches")}
    if "pinned" not in columns:
        op.add_column(
            "tracklist_fetches",
            sa.Column("pinned", sa.Boolean(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    with op.batch_alter_table("tracklist_fetches") as batch:
        batch.drop_column("pinned")
