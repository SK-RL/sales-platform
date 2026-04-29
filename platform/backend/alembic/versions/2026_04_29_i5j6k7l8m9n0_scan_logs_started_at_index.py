"""F277 — btree index on scan_logs.started_at for /monitoring + prune.

Revision ID: i5j6k7l8m9n0
Revises: h4i5j6k7l8m9
Create Date: 2026-04-29

Manual perf probe of /monitoring found the activity_24h aggregate
(``WHERE started_at >= last_24h``) was seq-scanning the entire
scan_logs table — no index existed on ``started_at`` despite that
column being the WHERE/ORDER BY anchor of every monitoring query
and the F272 prune task.

  EXPLAIN before:  Seq Scan on scan_logs (252k rows scanned, 38ms)
  EXPLAIN after:   Index Scan on idx_scan_logs_started_at_desc

Why DESC ordering on the index:
  * /monitoring "last scan" hits ``ORDER BY started_at DESC LIMIT 1``
  * /monitoring activity_24h does ``WHERE started_at >= cutoff
    ORDER BY started_at DESC`` (recent-events-first)
  * F272 prune does ``WHERE started_at < cutoff`` — bidirectional
    range scan works on any-direction btree
  * /platforms last_scan / total_errors aggregations are also
    "most recent" patterns
A plain ``btree(started_at)`` would work too, but DESC matches the
dominant access pattern exactly so the planner doesn't have to
reverse-scan.

The F272 retention window (60 days = ~780k rows steady-state) means
this index is load-bearing — without it /monitoring keeps paying
the full-table seq scan even AFTER prune lands. F272 was the
upstream fix; F277 makes the access path actually fast.

Idempotent via inspector check; safe to re-run if the index was
created manually as a hotfix.
"""

import sqlalchemy as sa
from alembic import op


revision = "i5j6k7l8m9n0"
down_revision = "h4i5j6k7l8m9"
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
    # DESC matches the dominant query pattern (most-recent-first).
    # ``IF NOT EXISTS`` guards re-runs against a DB where someone
    # created the index manually as a hotfix.
    if not _index_exists("scan_logs", "idx_scan_logs_started_at_desc"):
        op.execute(
            "CREATE INDEX idx_scan_logs_started_at_desc "
            "ON scan_logs USING btree (started_at DESC)"
        )


def downgrade() -> None:
    if _index_exists("scan_logs", "idx_scan_logs_started_at_desc"):
        op.execute("DROP INDEX idx_scan_logs_started_at_desc")
