"""F374 — aggregator apply-link resolution columns on jobs.

Himalayas is the catalogue's largest source (222k rows) and every one
is a repost: the real application form lives on the employer's ATS
behind an "Apply" redirect on the Himalayas page. These columns record
where that redirect lands, what ATS it is, and — when it is one we can
read — the catalogue Job created from it so the apply path uses the
real form. All nullable; no backfill.

Revision ID: t7u8v9w0x1y2
Revises: s6t7u8v9w0x1
"""

import sqlalchemy as sa
from alembic import op

revision = "t7u8v9w0x1y2"
down_revision = "s6t7u8v9w0x1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("apply_url", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("apply_platform", sa.String(length=50), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("resolved_job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("jobs", sa.Column("apply_resolve_status", sa.String(length=30), nullable=True))
    op.add_column("jobs", sa.Column("apply_resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("idx_jobs_resolved_job", "jobs", ["resolved_job_id"])


def downgrade() -> None:
    op.drop_index("idx_jobs_resolved_job", table_name="jobs")
    op.drop_column("jobs", "apply_resolved_at")
    op.drop_column("jobs", "apply_resolve_status")
    op.drop_column("jobs", "resolved_job_id")
    op.drop_column("jobs", "apply_platform")
    op.drop_column("jobs", "apply_url")
