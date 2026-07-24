"""Seed direct ATS boards for UAE-hub employers.

Revision ID: q3r4s5t6u7v8
Revises: p2q3r4s5t6u7
Create Date: 2026-07-24

UAE sourcing push, phase A (direct-employer boards). Live-probed the
public ATS APIs for ~50 UAE-native / UAE-hub employers on 2026-07-24;
most enterprise players (banks, telcos, Emaar/Aldar, G42) run
Oracle/SAP/Workday portals we don't scrape, but four high-volume
Greenhouse boards responded with live jobs and are missing or
badly under-covered in our corpus:

  slug     jobs on board   in our DB   coverage
  careem        28              0          0%   (Dubai HQ — ride-hail/super-app)
  okx          336             36         10%   (major Dubai hub — crypto exchange)
  bybit        126             56         44%   (Dubai HQ — crypto exchange)
  tamara        38              1          3%   (Riyadh HQ + Dubai — BNPL fintech)

The under-coverage exists because those rows arrived via aggregators
(Himalayas etc.) which carry only a slice of each employer's board.
Direct boards get the full listing + faster freshness (per-board
scans vs aggregator sweep).

Idempotent: existence-checked per (platform, slug) and per company
slug — a board that discovery already added is left untouched.
Column lists are introspected (same defence as o1p2q3r4s5t6 after
the relevance_score incident).
"""

import uuid

import sqlalchemy as sa
from alembic import op


revision = "q3r4s5t6u7v8"
down_revision = "p2q3r4s5t6u7"
branch_labels = None
depends_on = None


# (company name, company slug, ats platform, board slug, website)
UAE_BOARDS = [
    ("Careem", "careem", "greenhouse", "careem", "https://www.careem.com/"),
    ("OKX", "okx", "greenhouse", "okx", "https://www.okx.com/"),
    ("Bybit", "bybit", "greenhouse", "bybit", "https://www.bybit.com/"),
    ("Tamara", "tamara", "greenhouse", "tamara", "https://tamara.co/"),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    company_cols = {c["name"] for c in inspector.get_columns("companies")}
    board_cols = {c["name"] for c in inspector.get_columns("company_ats_boards")}

    for name, cslug, platform, bslug, website in UAE_BOARDS:
        # Board exists? Skip entirely (discovery may have beaten us).
        existing_board = bind.execute(
            sa.text(
                "SELECT id FROM company_ats_boards "
                "WHERE platform = :p AND slug = :s"
            ),
            {"p": platform, "s": bslug},
        ).scalar_one_or_none()
        if existing_board:
            continue

        # Find-or-create the company by slug.
        company_id = bind.execute(
            sa.text("SELECT id FROM companies WHERE slug = :slug"),
            {"slug": cslug},
        ).scalar_one_or_none()
        if not company_id:
            company_id = str(uuid.uuid4())
            candidates = [
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
            cols = [c for c, _ in candidates if c in company_cols]
            vals = [v for c, v in candidates if c in company_cols]
            bind.execute(
                sa.text(
                    f"INSERT INTO companies ({', '.join(cols)}) "
                    f"VALUES ({', '.join(vals)})"
                ),
                {
                    "id": company_id,
                    "name": name,
                    "slug": cslug,
                    "website": website,
                },
            )

        board_candidates = [
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
        bind.execute(
            sa.text(
                f"INSERT INTO company_ats_boards ({', '.join(bcols)}) "
                f"VALUES ({', '.join(bvals)})"
            ),
            {
                "id": str(uuid.uuid4()),
                "company_id": str(company_id),
                "platform": platform,
                "slug": bslug,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    for _, _, platform, bslug, _ in UAE_BOARDS:
        bind.execute(
            sa.text(
                "DELETE FROM company_ats_boards "
                "WHERE platform = :p AND slug = :s"
            ),
            {"p": platform, "s": bslug},
        )
    # Companies are left in place on downgrade — they may have
    # accumulated jobs/pipeline references we must not cascade away.
