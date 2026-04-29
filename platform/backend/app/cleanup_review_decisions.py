"""F279 — one-shot cleanup of legacy ``Review.decision`` values.

Background
----------
F73 added a ``decision_map`` in the review handler that normalizes
incoming verb-forms (``accept`` / ``reject`` / ``skip``) to canonical
past-tense (``accepted`` / ``rejected`` / ``skipped``) before persist.
Any row written before that landed kept its original verb-form. Live
audit at the time F110 was filed found 3 legacy rows
(``decision="accept"``) on the prod ``reviews`` table.

The forward path is fixed (handler normalises) and the analytics
endpoint is now defensive (filters to canonical values via the
F279 fix in ``analytics.py::review_insights``) so unfixed legacy
rows no longer skew dashboards. This script closes the loop by
backfilling those legacy rows so the column has a single canonical
vocabulary.

Why a script and not a migration
--------------------------------
A migration ``UPDATE reviews SET decision = 'accepted' WHERE
decision = 'accept'`` would also work, but:
  * The forward path has been fixed for months — there should be a
    handful of rows at most. A migration is overkill and locks the
    schema-version chain on a one-shot data fix.
  * Running this as an admin-triggered Python script lets the operator
    eyeball the ``before`` count, run the script, see the ``after`` count.
    Same shape as ``cleanup_review_tags.py`` (F73 sibling).
  * If a future round of testing finds MORE drift (a fourth verb form,
    say), we can re-run this without rolling out a new migration.

Usage
-----
::

    docker compose exec backend python -m app.cleanup_review_decisions
    docker compose exec backend python -m app.cleanup_review_decisions --dry-run

Idempotent: re-runs after the canonical-only fixed point are no-ops.
"""

from __future__ import annotations

import argparse
import logging
from sqlalchemy import select, update, func

from app.workers.tasks._db import SyncSession
from app.models.review import Review

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Mirrors the ``decision_map`` in ``app/api/v1/reviews.py``. Keep in
# sync — both encode the same legacy-to-canonical contract.
_LEGACY_TO_CANONICAL: dict[str, str] = {
    "accept": "accepted",
    "reject": "rejected",
    "skip": "skipped",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print before/after counts without writing.",
    )
    args = parser.parse_args()

    session = SyncSession()
    try:
        # Snapshot the current decision distribution so the operator
        # can sanity-check what the script saw vs what it changed.
        before = dict(
            session.execute(
                select(Review.decision, func.count(Review.id)).group_by(Review.decision)
            ).all()
        )
        logger.info("Decision distribution before: %s", before)

        legacy_total = sum(
            before.get(legacy, 0) for legacy in _LEGACY_TO_CANONICAL
        )
        if legacy_total == 0:
            logger.info("No legacy verb-form rows found — nothing to update.")
            return 0

        if args.dry_run:
            logger.info(
                "DRY RUN: would update %d rows (%s).",
                legacy_total,
                {k: before.get(k, 0) for k in _LEGACY_TO_CANONICAL},
            )
            return 0

        total_updated = 0
        for legacy, canonical in _LEGACY_TO_CANONICAL.items():
            result = session.execute(
                update(Review)
                .where(Review.decision == legacy)
                .values(decision=canonical)
            )
            updated = result.rowcount or 0
            if updated:
                logger.info(
                    "Backfilled %d rows: %r -> %r", updated, legacy, canonical
                )
            total_updated += updated
        session.commit()

        after = dict(
            session.execute(
                select(Review.decision, func.count(Review.id)).group_by(Review.decision)
            ).all()
        )
        logger.info("Decision distribution after: %s", after)
        logger.info("Total rows updated: %d", total_updated)
        return 0

    except Exception:
        logger.exception("cleanup_review_decisions failed; rolling back")
        session.rollback()
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
