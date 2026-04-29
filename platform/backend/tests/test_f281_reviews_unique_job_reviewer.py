"""F281 — UNIQUE(job_id, reviewer_id) on reviews (closes F156 race).

F156 found that concurrent ``POST /reviews`` for the same (job,
reviewer) pair race past the handler's check-then-write and
produce duplicate rows. Live probe at the time produced THREE
review rows from one user for one job, with two different
decisions — and on ``accepted`` decisions each duplicate spawned a
separate ``PotentialClient`` + flipped ``company.is_target=True``
+ queued a Celery task. Multiplied side-effects with no race-safe
gate to stop them.

The fix mirrors the F100 ResumeScore approach:
  (a) migration dedupes existing duplicates (most-recent-wins)
      then adds ``UNIQUE INDEX uq_reviews_job_reviewer``,
  (b) the model declares the same UniqueConstraint so a fresh
      ``Base.metadata.create_all()`` (test bootstrap, dev DB)
      gets the constraint too,
  (c) the handler catches the IntegrityError on race and surfaces
      it as a clean ``409 Conflict`` instead of a bare ``500``.

These tests are source-level. Live race verification is captured
in the migration docstring — running this against prod is the only
way to fully exercise the constraint, and that's covered by the
deploy-time smoke test.
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
os.environ.setdefault("JWT_SECRET", "pytest-f281")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _BACKEND / "alembic" / "versions"


def _find_migration() -> pathlib.Path:
    matches = list(_MIGRATIONS_DIR.glob("*_k7l8m9n0o1p2_*.py"))
    assert len(matches) == 1, (
        f"F281 regression: expected 1 migration with revision id "
        f"k7l8m9n0o1p2, found {len(matches)}: {matches}"
    )
    return matches[0]


def test_migration_chains_from_f278():
    """F281 must descend from F278's revision (``j6k7l8m9n0o1``)
    so alembic upgrade applies them in the right order.
    """
    src = _find_migration().read_text()
    assert 'revision = "k7l8m9n0o1p2"' in src
    assert 'down_revision = "j6k7l8m9n0o1"' in src


def test_migration_dedupes_before_unique_constraint():
    """Order matters: the dedupe DELETE must run BEFORE the
    UNIQUE INDEX is created, otherwise the index creation fails
    against existing duplicates. Verify both steps are present
    and the dedupe appears first textually.
    """
    src = _find_migration().read_text()
    # Slice on the upgrade() body only — the module docstring
    # legitimately mentions ``CREATE UNIQUE INDEX`` while explaining
    # the migration, which would confuse a positional check.
    upgrade_start = src.find("def upgrade()")
    upgrade_end = src.find("def downgrade()", upgrade_start)
    assert upgrade_start > 0 and upgrade_end > upgrade_start, (
        "F281 regression: couldn't locate ``upgrade()`` body in "
        "migration. File structure changed."
    )
    body = src[upgrade_start:upgrade_end]
    delete_pos = body.find("DELETE FROM reviews")
    index_pos = body.find("CREATE UNIQUE INDEX")
    assert delete_pos > 0, (
        "F281 regression: ``upgrade()`` no longer dedupes existing "
        "duplicates. The UNIQUE INDEX creation will fail at deploy "
        "time on any DB that has duplicate rows."
    )
    assert index_pos > 0, (
        "F281 regression: ``upgrade()`` no longer creates the UNIQUE "
        "INDEX. The race is back."
    )
    assert delete_pos < index_pos, (
        "F281 regression: UNIQUE INDEX appears before the dedupe "
        "DELETE in the upgrade body. Order matters: dedupe FIRST."
    )


def test_migration_keeps_most_recent_per_pair():
    """Most-recent-wins matches the user-visible "your latest
    decision is canonical" model. ``ROW_NUMBER() OVER (PARTITION
    BY job_id, reviewer_id ORDER BY created_at DESC, id DESC)``
    encodes that contract.
    """
    src = _find_migration().read_text()
    assert "PARTITION BY job_id, reviewer_id" in src, (
        "F281 regression: dedupe partition key changed. Without "
        "PARTITION BY (job_id, reviewer_id), the wrong rows get "
        "kept."
    )
    assert "ORDER BY created_at DESC" in src, (
        "F281 regression: dedupe order changed. Most-recent-first "
        "(created_at DESC) keeps the user's latest decision; "
        "any other order keeps the oldest, which is the opposite "
        "of the intended UX."
    )


def test_migration_index_is_unique():
    """A non-UNIQUE index would prevent the seq scan but NOT the
    race. The whole point is a DB-enforced uniqueness gate.
    """
    src = _find_migration().read_text()
    assert "CREATE UNIQUE INDEX" in src, (
        "F281 regression: index is no longer UNIQUE. A plain index "
        "speeds up reads but doesn't stop the race condition."
    )
    assert "uq_reviews_job_reviewer" in src, (
        "F281 regression: index name changed. Deploy probes look "
        "for ``uq_reviews_job_reviewer`` to confirm the constraint."
    )


def test_migration_idempotent():
    src = _find_migration().read_text()
    assert "_index_exists" in src or "IF NOT EXISTS" in src, (
        "F281 regression: migration lacks idempotency guard. "
        "Re-running ``alembic upgrade head`` after manual creation "
        "will error."
    )


def test_review_model_declares_unique_constraint():
    """Defense in depth — the model must declare the
    UniqueConstraint so a fresh ``Base.metadata.create_all()``
    (test bootstrap, dev DB) creates the constraint too. Without
    this, dev DBs silently allow duplicates until alembic catches up.
    """
    model_src = (_BACKEND / "app" / "models" / "review.py").read_text()
    assert "UniqueConstraint(" in model_src, (
        "F281 regression: Review model no longer declares "
        "``UniqueConstraint``. Fresh DBs (test fixtures, dev) will "
        "skip the race-safe gate."
    )
    assert "uq_reviews_job_reviewer" in model_src, (
        "F281 regression: Review model no longer references the "
        "F281 constraint by name — autogenerate would propose a "
        "duplicate constraint."
    )
    assert '"job_id", "reviewer_id"' in model_src or \
        "'job_id', 'reviewer_id'" in model_src, (
        "F281 regression: Review constraint columns drifted from "
        "the migration."
    )


def test_handler_translates_race_to_409():
    """The handler must catch ``IntegrityError`` from the UNIQUE
    constraint and re-raise as ``HTTPException(409)``. Without
    this, the race surfaces as a bare 500 with no actionable
    error message — the user sees an "Internal Server Error" when
    the actual cause is "you've already reviewed this job".
    """
    handler_src = (_BACKEND / "app" / "api" / "v1" / "reviews.py").read_text()
    assert "from sqlalchemy.exc import IntegrityError" in handler_src, (
        "F281 regression: ``IntegrityError`` import missing from "
        "reviews.py. The handler can't catch the race-violation "
        "without it."
    )
    assert "except IntegrityError" in handler_src, (
        "F281 regression: handler no longer catches "
        "``IntegrityError``. UNIQUE constraint violations now "
        "surface as bare 500s instead of clean 409s."
    )
    assert "status_code=409" in handler_src, (
        "F281 regression: handler no longer raises 409 on "
        "duplicate-review. The user sees a 500 instead of a "
        "useful 'already reviewed' message."
    )
    assert "uq_reviews_job_reviewer" in handler_src, (
        "F281 regression: handler doesn't reference the constraint "
        "name — without it, ANY IntegrityError gets converted to "
        "409, hiding genuinely-different bugs (FK violations, etc.)."
    )
