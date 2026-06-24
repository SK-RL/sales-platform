"""Seed the Adzuna UAE board so the scan picks it up automatically.

Revision ID: o1p2q3r4s5t6
Revises: n0o1p2q3r4s5
Create Date: 2026-05-24

Adds one Company + one CompanyATSBoard row so the existing scan
pipeline (which iterates ``company_ats_boards``) starts pulling UAE
jobs from Adzuna on its next tick — no operator action needed beyond
setting ``ADZUNA_APP_ID`` and ``ADZUNA_APP_KEY`` env vars on the VM.

Seed semantics:

  * The "company" row is a synthetic owner for the aggregator output
    (matches how Himalayas / RemoteOK / Y-Combinator WaaS register).
    ``name = "Adzuna (UAE)"`` so the admin board-list page shows what
    it is at a glance; ``slug = "adzuna-uae"`` is the parking slug
    for the synthetic Company row.

  * The board's ``slug`` column holds the **fetcher slug** (the value
    passed to ``AdzunaFetcher.fetch(slug)``), which for Adzuna is the
    ISO-3166 alpha-2 country code — ``"ae"`` for UAE.

  * ``is_active=true`` so the scan picks it up immediately. The
    ``fetched_at`` field stays NULL until the first scan completes.

  * The synthetic Company is marked ``is_target=false`` (no special
    pipeline boosting) and ``relevance_score=0`` (it's a virtual
    container, not a real employer the team would score). Real
    employer Companies get created on-the-fly by the scan task when
    each Adzuna row's ``company.display_name`` resolves to a slug.

Idempotent via existence checks — ``alembic upgrade head`` re-runs
are a no-op once the seed rows are present.

Adding more countries later (Saudi, India, Egypt, Bahrain, Oman,
Qatar, Kuwait) is a data change: insert one more
``CompanyATSBoard`` row per country with ``platform="adzuna"`` and
the right alpha-2 slug. No code change.
"""

import sqlalchemy as sa
from alembic import op


revision = "o1p2q3r4s5t6"
down_revision = "n0o1p2q3r4s5"
branch_labels = None
depends_on = None


# Stable UUIDs for the synthetic seed rows so re-running the migration
# on a different environment (staging vs prod) produces the same ids
# everywhere — makes ``DELETE`` rollback safe and lets cross-env
# logs reference one canonical row.
ADZUNA_UAE_COMPANY_ID = "00000000-aaaa-0000-0000-00000000ae00"
ADZUNA_UAE_BOARD_ID = "00000000-aaaa-0000-0000-00000000ae01"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Company row — synthetic owner for the aggregator output.
    existing = bind.execute(
        sa.text("SELECT id FROM companies WHERE slug = :slug"),
        {"slug": "adzuna-uae"},
    ).scalar_one_or_none()
    if not existing:
        # Build the INSERT dynamically from the columns that actually
        # exist on the prod ``companies`` table. The original revision
        # of this migration hard-coded a ``relevance_score`` column
        # that doesn't exist on the table — Postgres rejected the
        # whole INSERT (``column "relevance_score" of relation
        # "companies" does not exist``), which failed the migration
        # and auto-rolled-back the deploy. Introspecting makes the
        # seed immune to schema drift in either direction: we only
        # ever name columns the table has, and supply safe values for
        # the ones that are NOT NULL without a usable server default.
        existing_cols = {c["name"] for c in inspector.get_columns("companies")}

        # Candidate (column, SQL-literal-or-bind) pairs. Only those
        # whose column exists on the table are included. ``now()`` is
        # a literal; everything else is a bound param or literal that
        # satisfies the model's NOT NULL + default contract.
        candidates: list[tuple[str, str]] = [
            ("id", ":id"),
            ("name", ":name"),
            ("slug", ":slug"),
            ("website", ":website"),
            ("logo_url", "''"),
            ("industry", "''"),
            ("employee_count", "''"),
            ("funding_stage", "''"),
            ("headquarters", "''"),
            ("description", "''"),
            ("is_target", "false"),
            ("tags", "'{}'::text[]"),
            ("metadata_json", "'{}'::jsonb"),
            ("domain", "''"),
            ("total_funding", "''"),
            ("linkedin_url", "''"),
            ("twitter_url", "''"),
            ("tech_stack", "'{}'::text[]"),
            ("enrichment_status", "'pending'"),
            ("enrichment_error", "''"),
            ("funding_news_url", "''"),
            ("created_at", "now()"),
            ("updated_at", "now()"),
        ]
        cols = [c for c, _ in candidates if c in existing_cols]
        vals = [v for c, v in candidates if c in existing_cols]
        stmt = (
            f"INSERT INTO companies ({', '.join(cols)}) "
            f"VALUES ({', '.join(vals)})"
        )
        bind.execute(
            sa.text(stmt),
            {
                "id": ADZUNA_UAE_COMPANY_ID,
                "name": "Adzuna (UAE)",
                "slug": "adzuna-uae",
                "website": "https://www.adzuna.ae/",
            },
        )

    # Board row — the scan iterates this table; one row = one fetch call.
    existing_board = bind.execute(
        sa.text(
            "SELECT id FROM company_ats_boards "
            "WHERE platform = :platform AND slug = :slug"
        ),
        {"platform": "adzuna", "slug": "ae"},
    ).scalar_one_or_none()
    if not existing_board:
        # Same introspection guard as the company insert above — only
        # name columns the board table actually has, so a schema drift
        # on ``company_ats_boards`` can't fail the migration.
        board_cols = {c["name"] for c in inspector.get_columns("company_ats_boards")}
        board_candidates: list[tuple[str, str]] = [
            ("id", ":id"),
            ("company_id", ":company_id"),
            ("platform", ":platform"),
            ("slug", ":slug"),
            ("is_active", "true"),
            ("consecutive_zero_scans", "0"),
            ("deactivated_reason", "''"),
        ]
        bcols = [c for c, _ in board_candidates if c in board_cols]
        bvals = [v for c, v in board_candidates if c in board_cols]
        bstmt = (
            f"INSERT INTO company_ats_boards ({', '.join(bcols)}) "
            f"VALUES ({', '.join(bvals)})"
        )
        bind.execute(
            sa.text(bstmt),
            {
                "id": ADZUNA_UAE_BOARD_ID,
                "company_id": ADZUNA_UAE_COMPANY_ID,
                "platform": "adzuna",
                "slug": "ae",
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM company_ats_boards "
            "WHERE platform = 'adzuna' AND slug = 'ae'"
        )
    )
    bind.execute(
        sa.text("DELETE FROM companies WHERE slug = 'adzuna-uae'")
    )
