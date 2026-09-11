"""F374 — resolve aggregator postings to their employer forms, hourly.

Politeness over throughput: a small batch per run with a pause between
pages, highest-scoring first. The aggregator's own API is what the
scanner uses; this is the only path that touches its HTML pages, and
the outcome of each attempt is recorded so coverage — and any
challenge from the aggregator — is visible in the numbers rather than
guessed.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.services.aggregator_resolver import AGGREGATOR_PLATFORMS, resolve_job
from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession

logger = logging.getLogger(__name__)

BATCH = 40
PAUSE_SECONDS = 1.5
MIN_SCORE = 70
_STATUSES = ("new", "under_review", "accepted")


@celery_app.task
def resolve_aggregator_links(limit: int = BATCH) -> dict:
    from app.models.job import Job

    session = SyncSession()
    counts: dict[str, int] = {}
    try:
        jobs = session.execute(
            select(Job)
            .where(
                Job.platform.in_(list(AGGREGATOR_PLATFORMS)),
                Job.apply_resolved_at.is_(None),
                Job.relevance_score >= MIN_SCORE,
                Job.status.in_(_STATUSES),
            )
            .order_by(Job.relevance_score.desc(), Job.first_seen_at.desc())
            .limit(limit)
        ).scalars().all()
        for i, job in enumerate(jobs):
            try:
                out = resolve_job(session, job)
                counts[out["status"]] = counts.get(out["status"], 0) + 1
            except Exception:
                session.rollback()
                logger.warning("aggregator: unexpected failure on job %s", job.id, exc_info=True)
                counts["error"] = counts.get("error", 0) + 1
            if i < len(jobs) - 1:
                time.sleep(PAUSE_SECONDS)
        logger.info("aggregator: resolved %d job(s): %s", len(jobs), counts)
        return {"processed": len(jobs), **counts}
    finally:
        session.close()


@celery_app.task
def resolve_one_aggregator_job(job_id: str) -> dict:
    from app.models.job import Job

    session = SyncSession()
    try:
        job = session.get(Job, job_id)
        if job is None:
            return {"status": "not_found"}
        return resolve_job(session, job)
    finally:
        session.close()
