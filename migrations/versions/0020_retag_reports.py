"""preview and result of re-tagging an album

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-09 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "retag_reports" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "retag_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("album_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("options_json", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["album_id"],
            ["library_albums.id"],
            name=op.f("fk_retag_reports_album_id_library_albums"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_retag_reports_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retag_reports")),
        sa.UniqueConstraint("album_id", name=op.f("uq_retag_reports_album_id")),
    )


def downgrade() -> None:
    op.drop_table("retag_reports")
