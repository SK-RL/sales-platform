"""F316 — archive duplicate jobs + partial UNIQUE on (company_id, lower(trim(title))).

Revision ID: m9n0o1p2q3r4
Revises: l8m9n0o1p2q3
Create Date: 2026-05-05

Live operator report: ``we are having duplicate jobs``. Audit
of the scan-task upsert path found three gaps that allow a
``(company_id, normalised_title)`` pair to slip past the
existing F88 + cross-platform soft-match dedup logic:

  (a) F88's exact-title match (``Job.title == title``) is
      case-and-whitespace SENSITIVE. ``"Senior SRE"`` and
      ``"senior sre  "`` from two ATS sources don't collide.
  (b) The cross-platform soft-match (``title_normalized``) is
      SKIPPED when ``title_normalized`` is empty — i.e. for
      every UNCLASSIFIED job. Unclassified roles posted on two
      platforms accumulate as separate rows.
  (c) The handler's "lookup-then-insert" pattern is TOCTOU-
      vulnerable to concurrent scans even when both branches
      above would have caught the dup serially.

This migration ships the data-level fix mirroring F281
(reviews) and F282 (company_contacts):

  1. **Archive** older duplicates (``status='archived'``) per
     ``(company_id, lower(trim(title)))`` group, keeping the
     row with the highest ``first_seen_at`` (tie-breaks on
     ``id`` desc) as the active survivor. Only collapses ACTIVE
     rows — historical archived/expired/rejected rows stay
     untouched so a previously-rejected role re-listed today
     doesn't accidentally collapse with the new active one.

     Archiving (vs. deleting) preserves user data on the
     duplicates: reviews / applications / resume_scores stay
     attached to the dup-job row, the row just flips to
     ``archived`` status. The user's review list still shows
     the review with a link to the archived job. No CASCADE
     deletes, no FK reassignment, no UNIQUE-constraint conflicts
     downstream.

  2. **Add partial UNIQUE INDEX** on
     ``(company_id, lower(trim(title)))`` filtered to active
     statuses. Future scans that try to insert a dup get
     ``IntegrityError`` at the DB layer — the handler catches
     it and re-fetches the active survivor (companion change
     in ``scan_task.py:_upsert_job``).

The constraint is the only race-safe gate; lookup-then-insert
in application code is always TOCTOU-vulnerable. Same shape as
F281's ``uq_reviews_job_reviewer`` and F282's
``uq_company_contacts_company_email``.

Idempotent: the partial UNIQUE has ``IF NOT EXISTS`` and the
archive UPDATE is a no-op on a clean DB (rank-1 rows aren't
touched).
"""

import sqlalchemy as sa
from alembic import op


revision = "m9n0o1p2q3r4"
down_revision = "l8m9n0o1p2q3"
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
    # 1. Archive dupes. ``ROW_NUMBER`` partitions by the same key
    #    the partial UNIQUE will enforce, so any row that would
    #    otherwise violate the constraint flips to archived first.
    #    ``ORDER BY first_seen_at DESC, id DESC`` keeps the most
    #    recent sighting as the survivor — matches the user-visible
    #    "your latest scrape wins" mental model and aligns with the
    #    F281/F282 most-recent-wins pattern.
    #
    #    Only collapses rows currently in ACTIVE statuses. A role
    #    archived/rejected/expired in the past stays as-is — it
    #    was the user's decision to retire it, and a fresh listing
    #    with the same title is legitimately new (post-rejection).
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY company_id, lower(trim(title))
                       ORDER BY first_seen_at DESC, id DESC
                   ) AS rn
            FROM jobs
            WHERE status IN ('new', 'under_review', 'accepted')
        )
        UPDATE jobs
        SET status = 'archived',
            expired_at = COALESCE(expired_at, NOW())
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )

    # 2. Partial UNIQUE INDEX. ``WHERE status IN (...)`` makes this
    #    a partial index — historical rejected/expired/archived
    #    rows can coexist with an active rematch (legitimate
    #    re-listing after rejection). New scans can't insert a
    #    second active row for the same (company, lower-trimmed
    #    title); the constraint forces them through the upsert
    #    path.
    if not _index_exists("jobs", "uq_jobs_active_company_title"):
        op.execute(
            "CREATE UNIQUE INDEX uq_jobs_active_company_title "
            "ON jobs (company_id, lower(trim(title))) "
            "WHERE status IN ('new', 'under_review', 'accepted')"
        )


def downgrade() -> None:
    if _index_exists("jobs", "uq_jobs_active_company_title"):
        op.execute("DROP INDEX uq_jobs_active_company_title")
    # The archive UPDATE is intentionally not reversed — we can't
    # tell which rows were originally archived for other reasons,
    # and unarchiving everything would re-create the duplicates the
    # constraint just gated against.
