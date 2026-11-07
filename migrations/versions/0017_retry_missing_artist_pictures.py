"""retry the artist pictures that were not found

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-08 12:00:00.000000

The Deezer search used to miss well known artists (their collaborations came first and "50 Cent"
was beyond the first ten results), and an artist without a picture is not looked up again for
90 days. Forget those lookups so that the better search tries them again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE artists SET image_checked_at = NULL WHERE image_url IS NULL"))


def downgrade() -> None:
    pass  # nothing to undo: the lookups simply happen again
