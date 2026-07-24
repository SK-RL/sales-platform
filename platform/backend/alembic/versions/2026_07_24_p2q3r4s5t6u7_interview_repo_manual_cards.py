"""Interview question repository + manual pipeline cards.

Revision ID: p2q3r4s5t6u7
Revises: o1p2q3r4s5t6
Create Date: 2026-07-24

Two open feature tickets in one schema change:

1. Ticket 8ef0e9c2 — Interview Question Repository. New table
   ``interview_question_sets``: one row per interview debrief
   (company, role, round, date, candidate, optional interviewer,
   the questions themselves). Searchable via ILIKE across
   company/role/questions — the corpus is human-entered debriefs
   (hundreds of rows, not millions), so no trigram index needed
   until proven otherwise.

2. Ticket bac45b42 — Manual pipeline card creation. One JSONB
   column ``potential_clients.manual_card`` holding the free-form
   card fields (JD link, applied identity, designation, salary
   current/expected, interviewer + interviewee contacts, JD
   description, details). JSONB over 12 discrete columns for the
   same reason ``routine_preferences`` is JSONB: the field set is
   product-driven and will drift; reading code treats missing keys
   as empty. Mandatory-field enforcement lives in the Pydantic
   schema (``ManualPipelineCardRequest``), not the DB.

Idempotent via inspector checks — safe to re-run after a partial
apply.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "p2q3r4s5t6u7"
down_revision = "o1p2q3r4s5t6"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    if not _column_exists("potential_clients", "manual_card"):
        op.add_column(
            "potential_clients",
            sa.Column(
                "manual_card",
                JSONB,
                server_default="{}",
                nullable=False,
            ),
        )

    if not _table_exists("interview_question_sets"):
        op.create_table(
            "interview_question_sets",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                # SET NULL, not CASCADE — the repository's value is
                # institutional memory; a departing user's debriefs
                # must outlive their account.
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("company_name", sa.String(300), nullable=False),
            sa.Column(
                "company_id",
                UUID(as_uuid=True),
                sa.ForeignKey("companies.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("job_role", sa.String(200), nullable=False),
            sa.Column("interview_round", sa.String(100), nullable=False),
            sa.Column("interview_date", sa.Date(), nullable=True),
            sa.Column(
                "candidate_name",
                sa.String(200),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "interviewer", sa.String(300), nullable=False, server_default=""
            ),
            sa.Column("questions", sa.Text(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        # The list view's default ordering + the most common filter.
        op.create_index(
            "ix_interview_qs_company_created",
            "interview_question_sets",
            ["company_name", "created_at"],
        )


def downgrade() -> None:
    if _table_exists("interview_question_sets"):
        op.drop_index(
            "ix_interview_qs_company_created",
            table_name="interview_question_sets",
        )
        op.drop_table("interview_question_sets")
    if _column_exists("potential_clients", "manual_card"):
        op.drop_column("potential_clients", "manual_card")
