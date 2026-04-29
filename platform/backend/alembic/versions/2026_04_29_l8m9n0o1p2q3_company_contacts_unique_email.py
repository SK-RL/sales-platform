"""F282 — dedupe + partial UNIQUE(company_id, lower(trim(email))) on company_contacts.

Revision ID: l8m9n0o1p2q3
Revises: k7l8m9n0o1p2
Create Date: 2026-04-29

F160 found that concurrent ``POST /companies/{cid}/contacts`` with
identical email both pass the handler-level dedup check and both
commit, producing duplicate contact rows. The handler's
``_email_already_exists`` SELECT-then-INSERT is a TOCTOU race —
the only race-safe gate is a DB constraint.

Partial expression unique index on
``(company_id, lower(trim(email))) WHERE trim(email) <> ''`` so:
  * The match honours the same case-insensitive + whitespace-
    trimmed semantics as the existing handler helper, so a row
    inserted as ``"Foo@Bar.com "`` collides with one inserted as
    ``"foo@bar.com"`` even before normalisation lands on writes.
  * The ``WHERE`` filter exempts contacts with no email — those
    are legitimately separate people with missing data; a plain
    UNIQUE would block multiple email-less contacts per company.

Two-step shape mirroring F100/F281: dedupe FIRST so the index
build doesn't fail on existing duplicates, then create the index.
Most-recent-wins via ``ROW_NUMBER() OVER (PARTITION BY ... ORDER
BY created_at DESC, id DESC)``. Sibling test enforces the order.

Idempotent via ``CREATE UNIQUE INDEX IF NOT EXISTS``-style
inspector check.
"""

import sqlalchemy as sa
from alembic import op


revision = "l8m9n0o1p2q3"
down_revision = "k7l8m9n0o1p2"
branch_labels = None
depends_on = None


def _index_exists(table: str, name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        return name in {ix["name"] for ix in inspector.get_indexes(table)}
    except Exception:
        return False


def upgrade() -> None:
    # 1. Dedupe. Partition by (company_id, normalised email) where
    #    email is non-empty; keep the most recent row, drop the rest.
    #    NB: ``trim(email) <> ''`` is checked at the partition source
    #    so empty-email rows aren't ranked against each other.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY company_id, lower(trim(email))
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM company_contacts
            WHERE trim(email) <> ''
        )
        DELETE FROM company_contacts
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )

    # 2. Partial expression unique index. The ``WHERE trim(email) <> ''``
    #    clause is what makes this a *partial* index — empty-email
    #    contacts are not gated, so multiple "no-email" entries per
    #    company stay legitimate.
    if not _index_exists("company_contacts", "uq_company_contacts_company_email"):
        op.execute(
            "CREATE UNIQUE INDEX uq_company_contacts_company_email "
            "ON company_contacts (company_id, lower(trim(email))) "
            "WHERE trim(email) <> ''"
        )


def downgrade() -> None:
    if _index_exists("company_contacts", "uq_company_contacts_company_email"):
        op.execute("DROP INDEX uq_company_contacts_company_email")
