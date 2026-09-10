"""F345 — add 'expired' to ck_jobs_status_allowlist.

`expire_stale_jobs` has never expired a single job. Live count today:

    status=new       285,254
    status=expired         0

The task itself is correct -- it selects jobs whose `last_seen_at` is
older than 14 days and sets `status="expired"`. But F99's allowlist
(migration o5j6k7l8m9n0, 2026-04-16) is::

    ("new", "under_review", "accepted", "rejected", "hidden", "archived")

`expired` is not in it. So every night the UPDATE violates
`ck_jobs_status_allowlist`, the task's `except` rolls back and re-raises,
and nothing is expired. The failure is invisible from the UI: jobs simply
accumulate at `new` forever.

Worse, that migration's step 1 (`UPDATE jobs SET status='new' WHERE
status NOT IN (allowed)`) rewrote any pre-existing `expired` rows back to
`new`, so the state was destroyed at the same moment it became
unwritable.

The consequence is not cosmetic. In a live sample of the operator's
working filter, 354 of 477 jobs (74%) had `last_seen_at` older than the
14-day threshold and should have been terminal. A manual sweep of 14
postings drawn from that filter found 9 already dead at the ATS -- "Job
not found" on Ashby, silent redirects to the board index on Greenhouse
and Lever. The job pool looks large and converts poorly because most of
it no longer exists.

Why widen the constraint rather than retarget the task
------------------------------------------------------
`app/schemas/job.py` says the intent was for the write and filter
vocabularies to converge "once the legacy 'expired' rows are migrated to
'archived'". Collapsing them is the wrong direction here: F316 uses
`archived` for *deduplicated* rows, and the `expired_at` column exists to
record that a posting fell off its board. Those are different facts, and
telling them apart is exactly what the operator needs. `expired` is also
already in `JobStatusFilter`, in the `Job.status` model docstring, and in
the frontend's `JobStatus` union -- the DB constraint and
`JobStatusLiteral` are the two places out of step.

`JobStatusLiteral` (the user-facing *write* vocabulary) is deliberately
left alone: `expired` is a system-set terminal state owned by the
maintenance worker, not something a user should be able to PATCH a job
into. The DB constraint is the wider guard; the API literal stays the
narrower subset.

Idempotent: drops the constraint by name if present, then recreates it.
"""

import sqlalchemy as sa
from alembic import op


revision = "s6t7u8v9w0x1"
down_revision = "r5s6t7u8v9w0"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_jobs_status_allowlist"

# Every value any code path writes to jobs.status. Verified by grep over
# app/ -- `expired` is written only by maintenance_task.expire_stale_jobs.
_ALLOWED = (
    "new",
    "under_review",
    "accepted",
    "rejected",
    "hidden",
    "archived",
    "expired",
)

# The pre-F345 allowlist, for downgrade().
_ALLOWED_BEFORE = (
    "new",
    "under_review",
    "accepted",
    "rejected",
    "hidden",
    "archived",
)


def _constraint_exists() -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint WHERE conname = :n AND conrelid = 'jobs'::regclass"
            ),
            {"n": _CONSTRAINT},
        ).scalar()
    )


def upgrade() -> None:
    allowed_sql = ", ".join(f"'{v}'" for v in _ALLOWED)
    if _constraint_exists():
        op.drop_constraint(_CONSTRAINT, "jobs", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "jobs", f"status IS NULL OR status IN ({allowed_sql})"
    )


def downgrade() -> None:
    # Narrowing the allowlist again would fail the ALTER on any rows the
    # now-working task has expired, so fold them into 'archived' first --
    # the destination the schema comment always intended for them.
    allowed_sql = ", ".join(f"'{v}'" for v in _ALLOWED_BEFORE)
    if _constraint_exists():
        op.drop_constraint(_CONSTRAINT, "jobs", type_="check")
    op.execute("UPDATE jobs SET status = 'archived' WHERE status = 'expired'")
    op.create_check_constraint(
        _CONSTRAINT, "jobs", f"status IS NULL OR status IN ({allowed_sql})"
    )
