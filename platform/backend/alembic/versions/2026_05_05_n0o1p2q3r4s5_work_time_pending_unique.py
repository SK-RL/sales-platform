"""F324 — partial UNIQUE on work_time_extension_requests pending-per-user.

Revision ID: n0o1p2q3r4s5
Revises: m9n0o1p2q3r4
Create Date: 2026-05-05

The user-facing extension-request submitter
(``POST /work-window/me/extension-requests``) does a
lookup-then-insert pattern: it SELECTs for an existing pending
row, raises 409 if one exists, otherwise INSERTs a new pending
row. Two concurrent POSTs from the same user (double-click on a
slow connection, browser-tab duplicate) both pass the SELECT,
both INSERT, and the admin queue ends up with 2 duplicate
pending rows instead of the documented "one pending at a time"
contract.

F324 ships a partial UNIQUE INDEX:
``UNIQUE(user_id) WHERE status = 'pending'`` so the second
INSERT fails at the DB layer. Companion handler change in
``api/v1/work_window.py`` catches the IntegrityError and
re-raises as the same 409 the lookup-check produces, so the
user-visible outcome is identical regardless of timing.

Same shape as F281 (reviews), F282 (contacts), F316 (jobs):
DB-level race-safe gate + handler IntegrityError translation.

Idempotent: existing pending rows are left as-is; the partial
UNIQUE only affects future inserts. If a DB already has 2+
pending rows for the same user (pre-F324 race victim), the
CREATE INDEX fails — operator runs the cleanup snippet in the
docstring before retrying.
"""

import sqlalchemy as sa
from alembic import op


revision = "n0o1p2q3r4s5"
down_revision = "m9n0o1p2q3r4"
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
    # Defensive cleanup of pre-F324 duplicates: keep the most-recent
    # pending request per user (matches the F281/F282 most-recent-
    # wins convention), mark the older ones 'expired' so they fall
    # off the admin queue + the user's "you have a pending request"
    # banner without losing audit history.
    #
    # Uses 'expired' rather than 'denied' so it's distinguishable in
    # forensic queries — these weren't admin-decided, they were
    # auto-cleaned to satisfy the new constraint.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY user_id
                       ORDER BY requested_at DESC, id DESC
                   ) AS rn
            FROM work_time_extension_requests
            WHERE status = 'pending'
        )
        UPDATE work_time_extension_requests
        SET status = 'expired',
            decision_note = COALESCE(NULLIF(decision_note, ''), 'auto-cleaned by F324 (duplicate pending)'),
            decided_at = COALESCE(decided_at, NOW())
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )

    if not _index_exists(
        "work_time_extension_requests", "uq_work_time_pending_per_user"
    ):
        op.execute(
            "CREATE UNIQUE INDEX uq_work_time_pending_per_user "
            "ON work_time_extension_requests (user_id) "
            "WHERE status = 'pending'"
        )


def downgrade() -> None:
    if _index_exists(
        "work_time_extension_requests", "uq_work_time_pending_per_user"
    ):
        op.execute("DROP INDEX uq_work_time_pending_per_user")
    # Don't undo the cleanup UPDATE — same reasoning as F316: we can't
    # tell post-hoc which rows were originally pending vs auto-cleaned,
    # and unmarking them would re-create the duplicates we just removed.
