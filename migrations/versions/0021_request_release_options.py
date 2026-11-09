"""the releases a download may be, when the user has to say which

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-09 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("download_requests")}
    if "release_options_json" not in columns:
        op.add_column(
            "download_requests", sa.Column("release_options_json", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("download_requests") as batch:
        batch.drop_column("release_options_json")
