"""user theme preference; missing-album types become a system setting

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "theme", sa.String(length=8), server_default=sa.text("'auto'"), nullable=False
            )
        )
    # Personal choices of missing-album types are gone: only the system-wide choice remains.
    op.execute("DELETE FROM settings WHERE key = 'missing_types' AND scope = 'user'")


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("theme")
