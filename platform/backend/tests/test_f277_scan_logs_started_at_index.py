"""F277 — scan_logs.started_at btree index migration structural test.

Companion to F272 (scan_logs retention). F272 added the prune task
but didn't touch the access path; /monitoring activity_24h, the
F272 prune itself, and /platforms last_scan all filter on
``started_at`` and were seq-scanning the entire table on every
call (252k rows pre-F272; 60-day retention = ~780k rows steady-
state, which is still a real cost on every monitoring page render).

The fix adds ``CREATE INDEX idx_scan_logs_started_at_desc ON
scan_logs USING btree (started_at DESC)``. DESC matches the
dominant most-recent-first access pattern so the planner doesn't
need to reverse-scan.

These tests verify the migration shape only. The actual
EXPLAIN-ANALYZE verification runs as a one-off live-DB probe at
deploy time — captured in the migration docstring.
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
os.environ.setdefault("JWT_SECRET", "pytest-f277")


_MIGRATIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
)


def _find_migration() -> pathlib.Path:
    matches = list(_MIGRATIONS_DIR.glob("*_i5j6k7l8m9n0_*.py"))
    assert len(matches) == 1, (
        f"F277 regression: expected 1 migration with revision id "
        f"i5j6k7l8m9n0, found {len(matches)}: {matches}"
    )
    return matches[0]


def test_migration_chains_from_f275():
    """F277 must descend from F275's revision (``h4i5j6k7l8m9``).
    Breaking the chain surfaces as alembic refusing to upgrade with
    a 'multiple heads' or 'missing parent' error at deploy time.
    """
    src = _find_migration().read_text()
    assert 'revision = "i5j6k7l8m9n0"' in src
    assert 'down_revision = "h4i5j6k7l8m9"' in src


def test_migration_creates_started_at_btree_index():
    """The index must be a btree on ``started_at``. NOT a GIN /
    expression index — range filters (>=, <, ORDER BY) want a
    plain btree.
    """
    src = _find_migration().read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if "CREATE INDEX" in ln or "USING btree" in ln
    ]
    code = " ".join(code_lines)
    assert "idx_scan_logs_started_at_desc" in code, (
        "F277 regression: index name changed. /monitoring perf "
        "probes look for ``idx_scan_logs_started_at_desc`` in "
        "EXPLAIN output — renaming silently breaks those probes."
    )
    assert "USING btree" in code, (
        "F277 regression: index type is no longer btree. Range "
        "filters and ORDER BY need btree, not GIN/hash/etc."
    )
    assert "started_at" in code, (
        "F277 regression: index column is no longer ``started_at``. "
        "/monitoring + F272 prune still filter on started_at; "
        "wrong-column index won't fire."
    )


def test_migration_index_is_desc_ordered():
    """The dominant access pattern is most-recent-first
    (``ORDER BY started_at DESC LIMIT 1`` for last_scan,
    ``WHERE started_at >= cutoff ORDER BY started_at DESC`` for
    activity_24h). DESC ordering on the btree matches that
    pattern exactly so the planner doesn't reverse-scan.

    A plain ``btree(started_at)`` would also work — Postgres can
    scan in either direction — but the DESC order is the
    documented intent and we want regressions to surface.
    """
    src = _find_migration().read_text()
    assert "started_at DESC" in src, (
        "F277 regression: index is no longer DESC-ordered. The "
        "dominant access pattern is most-recent-first — DESC "
        "ordering keeps the planner from reverse-scanning."
    )


def test_migration_idempotent():
    """Re-running ``alembic upgrade head`` after manual creation
    must not error. The index can plausibly be created manually as
    a hotfix while this migration is being prepared.
    """
    src = _find_migration().read_text()
    assert "_index_exists" in src or "IF NOT EXISTS" in src, (
        "F277 regression: migration lacks an idempotency guard. "
        "Re-running ``alembic upgrade head`` after the index was "
        "created manually will error with 'index already exists'."
    )


def test_model_marks_started_at_indexed():
    """Defense in depth — the ScanLog model must declare
    ``index=True`` on started_at so that a future
    ``Base.metadata.create_all()`` (test bootstrap, fresh dev DB)
    creates the index too. Without this, a fresh dev/test DB
    would silently regress to seq-scan-on-every-monitoring-render
    until alembic upgrade ran.
    """
    model_src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app" / "models" / "scan.py"
    ).read_text()
    # Find the started_at column definition and confirm index=True.
    # The body of the mapped_column may span multiple lines so we
    # slice on a generous window around the started_at anchor.
    anchor = model_src.find("started_at")
    assert anchor >= 0, (
        "F277 regression: ScanLog.started_at column was removed "
        "or renamed. The migration index would now point to a "
        "non-existent column."
    )
    window = model_src[anchor:anchor + 400]
    assert "index=True" in window, (
        "F277 regression: ScanLog.started_at no longer has "
        "``index=True``. A fresh DB bootstrap (e.g. test fixture "
        "via ``Base.metadata.create_all``) will skip the index "
        "and regress to seq-scan-on-monitoring-render."
    )
