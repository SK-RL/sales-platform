"""F316 — jobs dedupe + partial UNIQUE on (company_id, lower(trim(title))).

Live operator report: ``we are having duplicate jobs``. Audit found
three gaps in the existing F88 + cross-platform soft-match dedup:
  (a) F88's exact-title match was case + whitespace SENSITIVE;
      ``"Senior SRE"`` and ``"senior sre  "`` from two ATS
      sources didn't collide.
  (b) The cross-platform soft-match (``title_normalized``) is
      SKIPPED for unclassified jobs (empty ``title_normalized``).
  (c) Lookup-then-insert is TOCTOU-vulnerable to concurrent scans.

F316 ships:
  1. Migration archives older duplicates per
     ``(company_id, lower(trim(title)))`` group, keeping
     most-recent active row as survivor; flips others to
     ``status='archived'``. No CASCADE deletes — reviews,
     applications, resume_scores stay attached to the dup-row
     for user-history preservation.
  2. Partial UNIQUE INDEX ``uq_jobs_active_company_title``
     filtered to active statuses. New scans can't insert dups;
     constraint forces handler through the upsert path.
  3. Handler hardening: F88 fallback now case-insensitive +
     active-status filtered (matches the constraint predicate);
     INSERT path catches IntegrityError on the constraint and
     re-fetches the survivor (race-safe gate).
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
os.environ.setdefault("JWT_SECRET", "pytest-f316")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _BACKEND / "alembic" / "versions"


def _find_migration() -> pathlib.Path:
    matches = list(_MIGRATIONS_DIR.glob("*_m9n0o1p2q3r4_*.py"))
    assert len(matches) == 1, (
        f"F316 regression: expected 1 migration with revision id "
        f"m9n0o1p2q3r4, found {len(matches)}: {matches}"
    )
    return matches[0]


def test_migration_chains_from_l8m9n0o1p2q3():
    src = _find_migration().read_text()
    assert 'revision = "m9n0o1p2q3r4"' in src
    assert 'down_revision = "l8m9n0o1p2q3"' in src


def test_migration_archives_dupes_keeping_most_recent():
    """Archives by ``ROW_NUMBER`` partitioned on the constraint
    key, ordered DESC by ``first_seen_at`` so the most recent
    active row survives. Matches F281/F282 most-recent-wins.
    """
    src = _find_migration().read_text()
    upgrade_start = src.find("def upgrade()")
    upgrade_end = src.find("def downgrade()", upgrade_start)
    body = src[upgrade_start:upgrade_end]
    assert "PARTITION BY company_id, lower(trim(title))" in body, (
        "F316 regression: dedupe partition key drifted from "
        "(company_id, lower(trim(title))). Constraint and dedupe "
        "must use the SAME key or the migration's archive pass "
        "won't satisfy the partial UNIQUE on creation."
    )
    assert "ORDER BY first_seen_at DESC, id DESC" in body, (
        "F316 regression: dedupe order drifted. Most-recent-wins "
        "is the documented contract."
    )
    assert "SET status = 'archived'" in body, (
        "F316 regression: dupes are no longer being archived. If "
        "they're being deleted instead, user reviews/applications "
        "on the dup rows cascade-delete — data loss."
    )


def test_migration_archives_only_active_rows():
    """Only collapses ACTIVE rows. Previously-rejected/expired
    rows stay untouched so a re-listed role doesn't accidentally
    collapse with the new active one.
    """
    src = _find_migration().read_text()
    upgrade_start = src.find("def upgrade()")
    upgrade_end = src.find("def downgrade()", upgrade_start)
    body = src[upgrade_start:upgrade_end]
    assert "WHERE status IN ('new', 'under_review', 'accepted')" in body, (
        "F316 regression: dedupe scope expanded beyond active "
        "rows. Historical rejected/expired rows would get "
        "double-archived."
    )


def test_migration_creates_partial_unique_index():
    """Partial UNIQUE filtered to active statuses — same predicate
    the dedupe pass uses, so the dataset satisfies the constraint
    by the time the index is created.
    """
    src = _find_migration().read_text()
    upgrade_start = src.find("def upgrade()")
    upgrade_end = src.find("def downgrade()", upgrade_start)
    body = src[upgrade_start:upgrade_end]
    assert "CREATE UNIQUE INDEX" in body
    assert "uq_jobs_active_company_title" in body
    assert "(company_id, lower(trim(title)))" in body, (
        "F316 regression: index expression drifted from "
        "``(company_id, lower(trim(title)))`` — won't match "
        "what the handler's case-insensitive F88 lookup checks."
    )
    # Partial WHERE clause is what makes this safe to ship — a
    # full UNIQUE would block re-listing a previously-rejected role.
    assert "WHERE status IN" in body


def test_migration_idempotent():
    src = _find_migration().read_text()
    assert "_index_exists" in src or "IF NOT EXISTS" in src


def test_handler_f88_fallback_case_insensitive():
    """The F88 fallback in ``_upsert_job`` must use
    ``func.lower(func.trim(Job.title))`` so case + whitespace
    variants collapse before the partial UNIQUE fires.
    """
    src = (_BACKEND / "app" / "workers" / "tasks" / "scan_task.py").read_text()
    handler_start = src.find("def _upsert_job(")
    handler_end = src.find("\ndef _scan_board", handler_start)
    body = src[handler_start:handler_end]
    assert "func.lower(func.trim(Job.title))" in body, (
        "F316 regression: F88 fallback no longer uses "
        "case-insensitive title comparison. Case/whitespace "
        "variants will fail the lookup, hit the partial UNIQUE "
        "instead, and bounce off the IntegrityError catch — "
        "wasteful and noisy."
    )


def test_handler_f88_fallback_filters_active_status():
    """F88 fallback must filter to active statuses to match the
    partial UNIQUE predicate. Otherwise a previously-rejected row
    can be returned as ``existing`` and the scan would 'update'
    that archived row instead of inserting a new active one.
    """
    src = (_BACKEND / "app" / "workers" / "tasks" / "scan_task.py").read_text()
    # Just check the handler body has the active-status tuple
    # near the F88 lookup.
    handler_start = src.find("def _upsert_job(")
    handler_end = src.find("\ndef _scan_board", handler_start)
    body = src[handler_start:handler_end]
    # Multiple occurrences possible; just confirm it appears
    # in the handler body at all (the cross-platform soft-match
    # also uses it, so >=1 occurrence after F316).
    assert body.count('Job.status.in_(("new", "under_review", "accepted"))') >= 2, (
        "F316 regression: F88 fallback no longer filters to "
        "active statuses. Mismatched scope vs the partial UNIQUE "
        "predicate."
    )


def test_handler_catches_integrity_error_on_constraint():
    """The INSERT path must catch ``IntegrityError`` and re-fetch
    the survivor when ``uq_jobs_active_company_title`` fires.
    Otherwise concurrent scans from sibling workers crash on the
    constraint and the whole batch's SAVEPOINT rolls back.
    """
    src = (_BACKEND / "app" / "workers" / "tasks" / "scan_task.py").read_text()
    handler_start = src.find("def _upsert_job(")
    handler_end = src.find("\ndef _scan_board", handler_start)
    body = src[handler_start:handler_end]
    assert "from sqlalchemy.exc import IntegrityError" in src, (
        "F316 regression: ``IntegrityError`` import missing — "
        "the constraint catch can't compile."
    )
    assert "except IntegrityError" in body, (
        "F316 regression: handler no longer catches "
        "IntegrityError. Constraint violations now crash the "
        "whole batch instead of falling through to the survivor "
        "update path."
    )
    assert "uq_jobs_active_company_title" in body, (
        "F316 regression: handler doesn't reference the "
        "constraint name — ANY IntegrityError gets converted "
        "to a survivor update, hiding genuinely-different bugs "
        "(FK violations, etc.)."
    )
