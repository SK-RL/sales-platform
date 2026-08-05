"""Create user_notices table (in-app login banners).

Revision ID: r5s6t7u8v9w0
Revises: q3r4s5t6u7v8
Create Date: 2026-08-05

Backs the per-user login-notice banner (app/models/user_notice.py).
First use: notify the two admins who manage the KYC profiles whose
uploaded files were lost in the storage-persistence incident
(feedback 650514ad) to re-upload them. Generic enough to reuse for any
future admin->user in-app message.

Idempotent: guarded on table existence so a re-run (or a partially
applied deploy) is a no-op rather than a hard failure.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision = "r5s6t7u8v9w0"
down_revision = "q3r4s5t6u7v8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_notices" in insp.get_table_names():
        return

    op.create_table(
        "user_notices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("level", sa.String(20), nullable=False, server_default="info"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("action_label", sa.String(100), nullable=True),
        sa.Column("action_href", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The hot query is "undismissed notices for this user" on every page
    # load — index the user_id it filters on.
    op.create_index("ix_user_notices_user_id", "user_notices", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_notices" not in insp.get_table_names():
        return
    op.drop_index("ix_user_notices_user_id", table_name="user_notices")
    op.drop_table("user_notices")
