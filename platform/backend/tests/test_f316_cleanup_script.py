"""F316 — cleanup_duplicate_jobs.py companion script (re-runnable dedupe).

Migration ``m9n0o1p2q3r4`` runs the dedupe ONCE at upgrade time.
``app/cleanup_duplicate_jobs.py`` is the re-runnable companion for
ops to use between deploys (e.g. when a new aggregator board
onboarded between deploys creates a fresh batch of duplicates
that the partial UNIQUE rejects on next scan but doesn't
retroactively clean up).

Tests verify the script:
  - exists at the expected path
  - mirrors the migration's CTE (same partition + order)
  - supports ``--dry-run`` (operator safety net)
  - flips status to 'archived' rather than DELETE (preserves
    user reviews/applications attached to the dup-row)
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
os.environ.setdefault("JWT_SECRET", "pytest-f316c")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_cleanup_script_exists():
    path = _BACKEND / "app" / "cleanup_duplicate_jobs.py"
    assert path.exists(), (
        "F316 regression: cleanup_duplicate_jobs.py is missing — "
        "ops have no re-runnable tool to dedupe between deploys."
    )


def test_cleanup_supports_dry_run():
    src = (_BACKEND / "app" / "cleanup_duplicate_jobs.py").read_text()
    assert "--dry-run" in src, (
        "F316 regression: ``--dry-run`` flag removed. Operators "
        "have no way to inspect the would-be archive count "
        "before committing."
    )


def test_cleanup_archives_does_not_delete():
    """Same invariant as the migration: flip status to 'archived'
    rather than DELETE. User reviews/applications/resume_scores
    on the dup-rows must stay attached (not cascade-deleted)."""
    src = (_BACKEND / "app" / "cleanup_duplicate_jobs.py").read_text()
    assert "SET status = 'archived'" in src, (
        "F316 regression: cleanup script no longer archives — "
        "if it's deleting, user data on the dup-rows cascades."
    )
    assert "DELETE FROM jobs" not in src, (
        "F316 regression: cleanup script reverted to DELETE — "
        "this loses the user reviews / applications / "
        "resume_scores attached to the dup rows."
    )


def test_cleanup_uses_same_partition_as_migration():
    """The script's CTE must match the migration's. Drift between
    them means the two paths produce different results on the
    same dataset.
    """
    script_src = (_BACKEND / "app" / "cleanup_duplicate_jobs.py").read_text()
    migrations_dir = _BACKEND / "alembic" / "versions"
    migration_files = list(migrations_dir.glob("*_m9n0o1p2q3r4_*.py"))
    assert migration_files, "F316 migration missing"
    migration_src = migration_files[0].read_text()
    # Both must use the same partition + order
    partition_clause = "PARTITION BY company_id, lower(trim(title))"
    order_clause = "ORDER BY first_seen_at DESC, id DESC"
    active_filter = "WHERE status IN ('new', 'under_review', 'accepted')"
    for clause in (partition_clause, order_clause, active_filter):
        assert clause in script_src, (
            f"F316 regression: cleanup script missing clause "
            f"``{clause}`` — drift from migration."
        )
        assert clause in migration_src, (
            f"F316 regression: migration missing clause "
            f"``{clause}`` — drift from cleanup script."
        )
