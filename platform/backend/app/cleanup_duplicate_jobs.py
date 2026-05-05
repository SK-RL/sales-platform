"""F316 — one-shot dedupe pass for duplicate Job rows.

Companion to migration ``m9n0o1p2q3r4`` which ships the partial
UNIQUE INDEX ``uq_jobs_active_company_title``. The migration's
own ``UPDATE jobs SET status='archived'`` pass handles the
upgrade-time cleanup, but this standalone script is the right
tool when ops want to re-run the dedupe between scans (e.g. if a
new aggregator board is added and produces a fresh batch of
duplicates that only the next migration would catch).

Why a script and not just trust the migration:
  * The migration runs once per deploy. New aggregator boards
    onboarded between deploys can introduce a fresh batch of
    duplicates that the partial UNIQUE will start REJECTING but
    won't retroactively clean up.
  * Re-running ``alembic upgrade`` against an already-up-to-date
    DB is a no-op — the dedupe pass inside ``upgrade()`` won't
    re-execute. This script gives ops a re-runnable tool.
  * Same shape + invariants as the migration's pass: most-recent-
    wins, archive (don't delete), active-status only.

Usage::

    docker compose exec backend python -m app.cleanup_duplicate_jobs --dry-run
    docker compose exec backend python -m app.cleanup_duplicate_jobs

Idempotent: re-runs after the canonical-only fixed point are
no-ops (the rank-1 archive is empty when every group has 1 row).
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import text

from app.workers.tasks._db import SyncSession

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Mirrors the migration's CTE. Kept in sync to ensure the script
# and the migration's archive pass produce identical results.
_DEDUPE_CTE_SQL = """
WITH ranked AS (
    SELECT id,
           company_id,
           title,
           ROW_NUMBER() OVER (
               PARTITION BY company_id, lower(trim(title))
               ORDER BY first_seen_at DESC, id DESC
           ) AS rn
    FROM jobs
    WHERE status IN ('new', 'under_review', 'accepted')
)
"""


def _count_dupes(session) -> int:
    """Count rows that would be archived (rank > 1 in the
    most-recent-wins partition). Same predicate the UPDATE uses,
    just SELECT'd.
    """
    result = session.execute(text(
        _DEDUPE_CTE_SQL
        + "SELECT COUNT(*) FROM ranked WHERE rn > 1"
    ))
    return int(result.scalar() or 0)


def _archive_dupes(session) -> int:
    """Flip dupes to ``status='archived'`` (and stamp
    ``expired_at`` if not already set). Returns rowcount.
    Mirrors the migration's UPDATE exactly so the two paths
    produce identical results on the same dataset.
    """
    result = session.execute(text(
        _DEDUPE_CTE_SQL
        + "UPDATE jobs "
          "SET status = 'archived', "
          "    expired_at = COALESCE(expired_at, NOW()) "
          "WHERE id IN (SELECT id FROM ranked WHERE rn > 1)"
    ))
    return result.rowcount or 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the dup count without writing.",
    )
    args = parser.parse_args()

    session = SyncSession()
    try:
        dup_count = _count_dupes(session)
        if dup_count == 0:
            logger.info("No duplicate active Job rows found — nothing to do.")
            return 0

        if args.dry_run:
            logger.info(
                "DRY RUN: would archive %d duplicate active Job rows "
                "(most-recent-wins per (company_id, lower(trim(title))) "
                "group; status flips to 'archived', user reviews/"
                "applications/resume_scores stay attached).",
                dup_count,
            )
            return 0

        archived = _archive_dupes(session)
        session.commit()
        logger.info(
            "Archived %d duplicate active Job rows. The partial UNIQUE "
            "uq_jobs_active_company_title now satisfied for all "
            "active groups.",
            archived,
        )
        return 0

    except Exception:
        logger.exception("cleanup_duplicate_jobs failed; rolling back")
        session.rollback()
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
