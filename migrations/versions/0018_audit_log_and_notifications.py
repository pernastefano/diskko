"""audit log and in-app notifications

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-08 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "audit_log" not in tables:
        op.create_table(
            "audit_log",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("at", sa.DateTime(), nullable=False),
            sa.Column("actor_id", sa.Integer(), nullable=True),
            sa.Column("actor_name", sa.String(length=64), nullable=False),
            sa.Column("action", sa.String(length=48), nullable=False),
            sa.Column("target_type", sa.String(length=24), nullable=True),
            sa.Column("target_id", sa.Integer(), nullable=True),
            sa.Column("target_label", sa.String(length=255), nullable=True),
            sa.Column("details_json", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(
                ["actor_id"],
                ["users.id"],
                name=op.f("fk_audit_log_actor_id_users"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        )
        with op.batch_alter_table("audit_log", schema=None) as batch_op:
            batch_op.create_index(batch_op.f("ix_audit_log_at"), ["at"])
            batch_op.create_index(batch_op.f("ix_audit_log_action"), ["action"])
            batch_op.create_index("ix_audit_log_actor_at", ["actor_id", "at"])
    if "notifications" not in tables:
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("request_id", sa.Integer(), nullable=True),
            sa.Column("album_label", sa.String(length=255), nullable=False),
            sa.Column("detail", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name=op.f("fk_notifications_user_id_users"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["request_id"],
                ["download_requests.id"],
                name=op.f("fk_notifications_request_id_download_requests"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
            sa.UniqueConstraint("request_id", "kind", name=op.f("uq_notifications_request_id")),
        )
        with op.batch_alter_table("notifications", schema=None) as batch_op:
            batch_op.create_index("ix_notifications_user_read", ["user_id", "read_at"])


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("audit_log")
