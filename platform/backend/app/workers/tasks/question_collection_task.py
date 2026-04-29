"""Nightly task to proactively collect ATS questions for recent jobs."""

import logging
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func
from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession
from app.models.job import Job
from app.models.job_question import JobQuestion
from app.models.company import CompanyATSBoard
from app.services.question_service import get_or_fetch_questions_sync

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = {"greenhouse", "lever", "ashby"}
MAX_PER_CYCLE = 100


@celery_app.task(name="app.workers.tasks.question_collection_task.collect_questions")
def collect_questions():
    """Proactively fetch and cache application questions for recent jobs."""
    logger.info("Starting collect_questions")
    session = SyncSession()

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)

        # F314 (data correctness, sibling to F313): use the
        # admin-configured RELEVANT clusters list instead of the
        # naive "any non-empty cluster" predicate. Pre-fix the
        # collector wasted Anthropic-quota / external-API calls
        # fetching questions for jobs in non-relevant clusters
        # (e.g. an admin-added ``data`` cluster marked
        # ``is_relevant=False``). Now matches the same
        # relevant-set used by /jobs?role_cluster=relevant +
        # /companies/scores + auto_target_companies (F313).
        from app.workers.tasks._role_matching import get_relevant_clusters_sync
        relevant_clusters = get_relevant_clusters_sync(session)

        # Find recent jobs on supported platforms that have no cached questions
        existing_job_ids = select(JobQuestion.job_id).distinct()

        jobs = session.execute(
            select(Job).where(
                Job.platform.in_(SUPPORTED_PLATFORMS),
                Job.first_seen_at >= cutoff,
                Job.status.in_(["new", "under_review", "accepted"]),
                Job.id.notin_(existing_job_ids),
                Job.role_cluster.in_(relevant_clusters),  # F314
            ).order_by(Job.relevance_score.desc()).limit(MAX_PER_CYCLE)
        ).scalars().all()

        fetched = 0
        errors = 0

        for job in jobs:
            # Find the board slug for this job
            board = session.execute(
                select(CompanyATSBoard).where(
                    CompanyATSBoard.company_id == job.company_id,
                    CompanyATSBoard.platform == job.platform,
                    CompanyATSBoard.is_active.is_(True),
                )
            ).scalar_one_or_none()

            if not board:
                continue

            try:
                questions = get_or_fetch_questions_sync(session, job, board.slug)
                if questions:
                    fetched += 1
                session.commit()
            except Exception as e:
                logger.warning("Failed to fetch questions for job %s: %s", job.id, e)
                session.rollback()
                errors += 1

        logger.info("collect_questions complete: %d fetched, %d errors", fetched, errors)
        return {"fetched": fetched, "errors": errors}

    except Exception as e:
        logger.exception("collect_questions failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()
