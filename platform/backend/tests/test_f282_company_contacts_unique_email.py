"""F282 — partial UNIQUE on company_contacts(company_id, lower(trim(email)))
where trim(email) <> '' (closes F160 race).

F160 found that concurrent ``POST /companies/{cid}/contacts`` with
identical email both pass the handler-level
``_email_already_exists`` SELECT-then-INSERT and both commit,
producing duplicate contact rows. The handler check is
TOCTOU-vulnerable; only a DB constraint is race-safe.

Partial expression index because:
  * ``WHERE trim(email) <> ''`` exempts no-email contacts —
    legitimately separate people with missing data, NOT a race.
  * ``lower(trim(email))`` matches the handler's normalisation
    semantics so a row inserted as ``"Foo@Bar.com "`` collides
    with one inserted as ``"foo@bar.com"``.

Three-part fix mirroring F281:
  (a) migration ``l8m9n0o1p2q3`` dedupes existing duplicates then
      adds the partial index.
  (b) model declares the same Index for fresh-DB symmetry.
  (c) handler catches IntegrityError on uq_company_contacts_*
      and surfaces 409 with the existing contact's id.
"""
from __future__ import annotations

import os
import pathlib

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f282")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _BACKEND / "alembic" / "versions"


def _find_migration() -> pathlib.Path:
    matches = list(_MIGRATIONS_DIR.glob("*_l8m9n0o1p2q3_*.py"))
    assert len(matches) == 1, (
        f"F282 regression: expected 1 migration with revision id "
        f"l8m9n0o1p2q3, found {len(matches)}: {matches}"
    )
    return matches[0]


def test_migration_chains_from_f281():
    src = _find_migration().read_text()
    assert 'revision = "l8m9n0o1p2q3"' in src
    assert 'down_revision = "k7l8m9n0o1p2"' in src


def test_migration_dedupes_before_index():
    """Order matters: dedupe FIRST, otherwise the partial unique
    index creation fails on existing duplicates.
    """
    src = _find_migration().read_text()
    upgrade_start = src.find("def upgrade()")
    upgrade_end = src.find("def downgrade()", upgrade_start)
    body = src[upgrade_start:upgrade_end]
    delete_pos = body.find("DELETE FROM company_contacts")
    index_pos = body.find("CREATE UNIQUE INDEX")
    assert delete_pos > 0
    assert index_pos > 0
    assert delete_pos < index_pos, (
        "F282 regression: UNIQUE INDEX appears before the dedupe "
        "DELETE in upgrade body. Order matters: dedupe FIRST."
    )


def test_migration_uses_partial_where_clause():
    """The ``WHERE trim(email) <> ''`` clause is what makes this a
    *partial* index. A plain UNIQUE would block multiple no-email
    contacts per company, breaking legitimate use cases.
    """
    src = _find_migration().read_text()
    upgrade_start = src.find("def upgrade()")
    upgrade_end = src.find("def downgrade()", upgrade_start)
    body = src[upgrade_start:upgrade_end]
    assert "WHERE trim(email)" in body, (
        "F282 regression: partial index no longer has a WHERE "
        "clause. A plain UNIQUE would block multiple email-less "
        "contacts at the same company."
    )


def test_migration_normalises_email_in_index_expression():
    """The unique key must use ``lower(trim(email))`` so the
    constraint matches the handler's ``_email_already_exists``
    semantics — otherwise a row inserted as ``"Foo@Bar.com "``
    would slip past the constraint despite colliding under
    normalisation.
    """
    src = _find_migration().read_text()
    upgrade_start = src.find("def upgrade()")
    upgrade_end = src.find("def downgrade()", upgrade_start)
    body = src[upgrade_start:upgrade_end]
    assert "lower(trim(email))" in body, (
        "F282 regression: index expression no longer normalises "
        "email. Constraint will fail to detect mixed-case + whitespace "
        "duplicates that the handler check would catch."
    )


def test_migration_dedupe_uses_most_recent_wins():
    """``ORDER BY created_at DESC, id DESC`` keeps the user's
    latest contact row when collapsing duplicates."""
    src = _find_migration().read_text()
    assert "ORDER BY created_at DESC" in src
    assert "PARTITION BY company_id, lower(trim(email))" in src


def test_migration_idempotent():
    src = _find_migration().read_text()
    assert "_index_exists" in src or "IF NOT EXISTS" in src


def test_company_contact_model_declares_partial_unique():
    """Fresh DB bootstrap (test fixtures, dev DB) must also create
    the partial unique index, otherwise dev environments silently
    allow the F160 race.
    """
    model_src = (_BACKEND / "app" / "models" / "company_contact.py").read_text()
    assert "uq_company_contacts_company_email" in model_src, (
        "F282 regression: company_contact model no longer declares "
        "the partial unique index by name."
    )
    assert "unique=True" in model_src, (
        "F282 regression: model index lost ``unique=True``."
    )
    assert "postgresql_where" in model_src, (
        "F282 regression: model index lost the ``postgresql_where`` "
        "filter, so fresh DBs would block multiple email-less "
        "contacts at the same company."
    )


def test_create_contact_handler_translates_race_to_409():
    """The handler must catch IntegrityError on the partial-unique
    constraint and surface it as a clean 409, matching the existing
    handler-check 409 shape (so the UI doesn't have to special-case
    the race path).
    """
    handler_src = (_BACKEND / "app" / "api" / "v1" / "companies.py").read_text()
    assert "from sqlalchemy.exc import IntegrityError" in handler_src
    # The translation must reference the constraint name so non-F160
    # IntegrityErrors (e.g. FK to deleted Company) still propagate as
    # genuine 500s for ops to debug.
    assert "uq_company_contacts_company_email" in handler_src, (
        "F282 regression: handler doesn't reference the constraint "
        "name. ANY IntegrityError would be converted to 409, hiding "
        "genuinely-different bugs."
    )
    # The 409 path must come back with the existing contact id so the
    # UI can deep-link to it (matches the handler-check 409 shape).
    assert "existing_contact_id" in handler_src, (
        "F282 regression: race-path 409 no longer returns "
        "``existing_contact_id``. UI can no longer link the user to "
        "the duplicate they tried to create."
    )
