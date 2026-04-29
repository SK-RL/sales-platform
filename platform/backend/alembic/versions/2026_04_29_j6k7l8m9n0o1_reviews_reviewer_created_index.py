"""F278 — composite index on reviews(reviewer_id, created_at DESC).

Revision ID: j6k7l8m9n0o1
Revises: i5j6k7l8m9n0
Create Date: 2026-04-29

Manual perf probe of the ai_insights nightly task and the
/reviews/queue page-load found the ``reviews`` table had ZERO
indexes (only the implicit pkey on ``id``), despite the dominant
read pattern being:

    WHERE reviewer_id = ? AND created_at >= ?
    ORDER BY created_at DESC

ai_insights_task fires nightly per active user with FOUR queries
shaped like that — each one currently seq-scans the entire
reviews table. /reviews/queue does the same shape on every page
render. As ``reviews`` grows (one row per (user, decision) — the
table grows linearly with engagement), this becomes the dominant
slow-query for the insights pipeline.

  EXPLAIN before:  Seq Scan on reviews (filter on reviewer_id +
                   created_at, full table)
  EXPLAIN after:   Index Scan on idx_reviews_reviewer_created

Composite shape rationale:
- ``reviewer_id`` first because it's the equality filter (most
  selective predicate); the planner can binary-search to the
  user's slice in O(log n).
- ``created_at DESC`` second because within a single user's slice
  the queries either filter ``>= cutoff`` (range scan, direction-
  agnostic) or ``ORDER BY created_at DESC`` (DESC matches scan
  direction so no reverse-sort needed).

This single index covers all four dominant access patterns:
  * ai_insights per-user activity 30d  (reviewer + cutoff)
  * ai_insights per-user rejection tags (reviewer + cutoff + decision)
  * /reviews/queue listing              (reviewer + ORDER BY created_at DESC)
  * audit "what did user X review when" (reviewer + cutoff)

We do NOT add an index on ``created_at`` alone — the few queries
that scan all-users-all-time (ai_insights product signals) are
weekly aggregates over small windows, not page-load critical.
Adding a global created_at index would just double write
amplification on every review insert with no read benefit.

Idempotent via inspector check; safe to re-run if someone created
the index manually as a hotfix.
"""

import sqlalchemy as sa
from alembic import op


revision = "j6k7l8m9n0o1"
down_revision = "i5j6k7l8m9n0"
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
    if not _index_exists("reviews", "idx_reviews_reviewer_created"):
        op.execute(
            "CREATE INDEX idx_reviews_reviewer_created "
            "ON reviews USING btree (reviewer_id, created_at DESC)"
        )


def downgrade() -> None:
    if _index_exists("reviews", "idx_reviews_reviewer_created"):
        op.execute("DROP INDEX idx_reviews_reviewer_created")
