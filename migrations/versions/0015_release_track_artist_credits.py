"""the artist credit of each track of a MusicBrainz track list

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-07 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("release_tracks")}
    if "artist_credit_json" not in columns:
        with op.batch_alter_table("release_tracks", schema=None) as batch_op:
            batch_op.add_column(sa.Column("artist_credit_json", sa.Text(), nullable=True))
    # The stored lists were read without the artists: forget when they were fetched (a cache),
    # so that each album reads its list again, with the artists, the next time it is opened.
    op.execute(sa.text("DELETE FROM tracklist_fetches"))


def downgrade() -> None:
    with op.batch_alter_table("release_tracks", schema=None) as batch_op:
        batch_op.drop_column("artist_credit_json")
