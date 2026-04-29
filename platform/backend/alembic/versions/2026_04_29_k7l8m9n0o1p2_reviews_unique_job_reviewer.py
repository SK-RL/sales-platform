"""F281 — dedupe reviews + UNIQUE(job_id, reviewer_id) (closes F156).

Revision ID: k7l8m9n0o1p2
Revises: j6k7l8m9n0o1
Create Date: 2026-04-29

F156 found that concurrent ``POST /reviews`` submissions for the
same (job, reviewer) pair race past the handler's plain
``db.add(review)`` and produce duplicate Review rows. Live probe
at the time produced THREE reviews for the same job from the same
reviewer with two different decisions; on ``accepted`` decisions
each duplicate spawned a separate ``PotentialClient`` + flipped
``company.is_target=True`` + queued a Celery feedback task —
multiplying the side-effects.

Two-step migration, mirroring the F100 ``ResumeScore`` pattern
(``p6k7l8m9n0o1``):

1. Dedupe in place — ``ROW_NUMBER() OVER (PARTITION BY job_id,
   reviewer_id ORDER BY created_at DESC, id DESC)``, keep rank 1,
   delete the rest. Most-recent-wins matches the user-visible
   "your latest decision is the canonical one" mental model.

2. Add ``UNIQUE INDEX uq_reviews_job_reviewer`` so any future
   write that bypasses the handler's normalization (or wins a
   race on the handler-side check) fails loudly at the DB
   instead of silently creating drift again.

Order matters: dedupe BEFORE the constraint, otherwise the
ALTER TABLE fails against the existing duplicates.

Idempotent: re-running after the canonical-only fixed point is a
no-op (rank-1 delete drops zero rows; CREATE UNIQUE INDEX IF NOT
EXISTS is no-op on second run).
"""

import sqlalchemy as sa
from alembic import op


revision = "k7l8m9n0o1p2"
down_revision = "j6k7l8m9n0o1"
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
    # 1. Dedupe. Keep the most recent row per (job_id, reviewer_id)
    #    pair; delete the rest. ``id DESC`` tiebreaker for the
    #    rare same-millisecond race so the result is deterministic.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY job_id, reviewer_id
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM reviews
        )
        DELETE FROM reviews
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )

    # 2. UNIQUE index. ``IF NOT EXISTS`` so reruns are safe.
    if not _index_exists("reviews", "uq_reviews_job_reviewer"):
        op.execute(
            "CREATE UNIQUE INDEX uq_reviews_job_reviewer "
            "ON reviews (job_id, reviewer_id)"
        )


def downgrade() -> None:
    # Drop the unique index. The dedupe is not reversible — we can't
    # reconstruct rows we deleted, and downgrade is a rollback path,
    # not a restore path.
    if _index_exists("reviews", "uq_reviews_job_reviewer"):
        op.execute("DROP INDEX uq_reviews_job_reviewer")
