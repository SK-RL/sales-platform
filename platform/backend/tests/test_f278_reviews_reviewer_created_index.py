"""F278 — reviews(reviewer_id, created_at DESC) composite index test.

Companion to F277 (scan_logs.started_at). The reviews table had
ZERO indexes despite the dominant read pattern being:

    WHERE reviewer_id = ? AND created_at >= cutoff
    ORDER BY created_at DESC

ai_insights_task fires nightly per active user with FOUR queries
shaped like that — each one was seq-scanning the entire reviews
table. /reviews/queue does the same shape on every page render.

The composite ``(reviewer_id, created_at DESC)`` covers all four
dominant access patterns in a single index. ``reviewer_id`` first
because it's the equality filter (most selective); ``created_at
DESC`` second so the most-recent-first ordering matches scan
direction without a reverse sort.

These tests verify the migration shape only. Live EXPLAIN
verification at deploy time is captured in the migration docstring.
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
os.environ.setdefault("JWT_SECRET", "pytest-f278")


_MIGRATIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
)


def _find_migration() -> pathlib.Path:
    matches = list(_MIGRATIONS_DIR.glob("*_j6k7l8m9n0o1_*.py"))
    assert len(matches) == 1, (
        f"F278 regression: expected 1 migration with revision id "
        f"j6k7l8m9n0o1, found {len(matches)}: {matches}"
    )
    return matches[0]


def test_migration_chains_from_f277():
    """F278 must descend from F277's revision (``i5j6k7l8m9n0``).
    Breaking the chain surfaces as alembic refusing to upgrade with
    a 'multiple heads' or 'missing parent' error at deploy.
    """
    src = _find_migration().read_text()
    assert 'revision = "j6k7l8m9n0o1"' in src
    assert 'down_revision = "i5j6k7l8m9n0"' in src


def test_migration_creates_composite_btree_index():
    """The index must be a btree composite on
    (reviewer_id, created_at). Both columns matter — single-column
    on either column would miss the dominant access pattern.
    """
    src = _find_migration().read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if "CREATE INDEX" in ln or "USING btree" in ln
    ]
    code = " ".join(code_lines)
    assert "idx_reviews_reviewer_created" in code, (
        "F278 regression: index name changed. Deploy EXPLAIN probes "
        "look for ``idx_reviews_reviewer_created`` to confirm hit."
    )
    assert "USING btree" in code, (
        "F278 regression: index type is no longer btree. Equality "
        "+ range needs btree; GIN/hash don't support range scans."
    )
    assert "reviewer_id" in code and "created_at" in code, (
        "F278 regression: index is no longer composite on "
        "(reviewer_id, created_at). Single-column index won't "
        "cover the per-user-recent-activity query shape."
    )


def test_migration_index_orders_reviewer_first():
    """``reviewer_id`` must come BEFORE ``created_at`` in the index
    column list. The planner uses leftmost columns for equality
    filters; reversing the order makes the index useless for
    ``WHERE reviewer_id = X AND created_at >= cutoff`` because
    Postgres can't seek to a specific reviewer's slice without
    scanning all created_at ranges first.
    """
    src = _find_migration().read_text()
    # Find the (..., ...) column tuple inside the CREATE INDEX line.
    idx_line = next(
        (ln for ln in src.splitlines() if "CREATE INDEX" in ln
         or "(reviewer_id" in ln or "btree" in ln),
        "",
    )
    # Combine all relevant lines for the assertion.
    code = " ".join(
        ln for ln in src.splitlines()
        if "CREATE INDEX" in ln or "btree" in ln or "reviewer_id" in ln
    )
    reviewer_pos = code.find("reviewer_id")
    created_pos = code.find("created_at")
    assert reviewer_pos >= 0 and created_pos >= 0, (
        "F278 regression: couldn't locate column order in CREATE "
        "INDEX statement."
    )
    assert reviewer_pos < created_pos, (
        f"F278 regression: column order is reversed — created_at "
        f"appears before reviewer_id in the index. Reverse order "
        f"makes the index useless for the dominant ``WHERE "
        f"reviewer_id = X`` access pattern."
    )


def test_migration_index_is_desc_ordered():
    """``created_at DESC`` matches the most-recent-first ORDER BY
    in /reviews/queue and ai_insights — DESC ordering keeps the
    planner from reverse-scanning. A plain ASC btree would also
    work for range scans but loses the ORDER BY benefit.
    """
    src = _find_migration().read_text()
    assert "created_at DESC" in src, (
        "F278 regression: index is no longer DESC-ordered on "
        "created_at. Most-recent-first queries (/reviews/queue) "
        "now reverse-scan."
    )


def test_migration_idempotent():
    src = _find_migration().read_text()
    assert "_index_exists" in src or "IF NOT EXISTS" in src, (
        "F278 regression: migration lacks an idempotency guard. "
        "Re-running ``alembic upgrade head`` after the index was "
        "created manually will error."
    )


def test_review_model_declares_index_for_metadata_create_all():
    """The Review model must declare the composite index in
    ``__table_args__`` so a fresh ``Base.metadata.create_all()``
    (test bootstrap, dev DB) creates the index too. Without this,
    test fixtures and dev DBs silently regress to seq-scan-on-every-
    insights-run until alembic upgrade catches up.
    """
    model_src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app" / "models" / "review.py"
    ).read_text()
    assert "__table_args__" in model_src, (
        "F278 regression: Review model has no ``__table_args__``. "
        "Without it, ``Base.metadata.create_all()`` skips the "
        "composite index and fresh dev/test DBs seq-scan."
    )
    assert "idx_reviews_reviewer_created" in model_src, (
        "F278 regression: Review model no longer declares the "
        "F278 composite index by name. Migration + model must "
        "stay in sync; otherwise autogenerate proposes a duplicate."
    )
    assert "reviewer_id" in model_src and "created_at" in model_src, (
        "F278 regression: Review model index columns drifted from "
        "the migration."
    )
