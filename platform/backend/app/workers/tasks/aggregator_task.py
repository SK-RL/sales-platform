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
BLOCKED_STREAK_STOP = 3
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
                # unresolved, or walled before the company lookup existed
                (Job.apply_resolved_at.is_(None)) | (Job.apply_resolve_status == "blocked"),
                Job.relevance_score >= MIN_SCORE,
                Job.status.in_(_STATUSES),
            )
            .order_by(Job.relevance_score.desc(), Job.first_seen_at.desc())
            .limit(limit)
        ).scalars().all()
        blocked_streak = 0
        for i, job in enumerate(jobs):
            # Measured on production 2026-09-11: Himalayas answers the VM
            # with Cloudflare's challenge on every page (6/6), and a real
            # browser doesn't clear it either. After three in a row stop
            # fetching pages for this run; the company + title lookup
            # (F375) still runs, since it never touches the aggregator.
            skip_page = blocked_streak >= BLOCKED_STREAK_STOP
            try:
                out = resolve_job(session, job, skip_page=skip_page)
                counts[out["status"]] = counts.get(out["status"], 0) + 1
                page_blocked = out.get("detail", "").startswith("aggregator challenged") or skip_page
                blocked_streak = blocked_streak + 1 if page_blocked else 0
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
