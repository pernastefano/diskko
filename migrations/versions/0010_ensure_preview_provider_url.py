"""make sure preview_lookups has provider_url

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Migration 0008 was edited (it gained this column) after a development database had
    # already applied its first version, so that database lacks the column. A fresh database
    # has it from 0008: this step does nothing there.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("preview_lookups")}
    if "provider_url" not in columns:
        with op.batch_alter_table("preview_lookups", schema=None) as batch_op:
            batch_op.add_column(sa.Column("provider_url", sa.Text(), nullable=True))


def downgrade() -> None:
    pass  # the column belongs to 0008; downgrading that revision drops the whole table
