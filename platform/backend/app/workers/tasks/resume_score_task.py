"""Celery task for scoring a resume against all relevant jobs."""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession
from app.workers.tasks._ats_scoring import compute_ats_score
from app.models.resume import Resume, ResumeScore
from app.models.job import Job, JobDescription
from app.models.role_config import RoleClusterConfig
from app.models.user import User
from app.utils.job_description import extract_description
# F315 (consolidation): re-use the canonical sync helper from
# ``_role_matching.get_relevant_clusters_sync`` (shipped in F313).
# Pre-fix this file had its own duplicate ``_get_relevant_clusters_sync``
# — same query shape, but two sources of truth meant a future
# tweak to the fallback contract (e.g. expanding the default pair
# beyond ``["infra", "security"]``) had to be applied in two places.
# Now there's one helper and the rest of the codebase imports it.
from app.workers.tasks._role_matching import get_relevant_clusters_sync

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks.resume_score_task.score_resume_task", bind=True, max_retries=1)
def score_resume_task(self, resume_id: str):
    """Score a resume against all relevant jobs in the background."""
    session = SyncSession()
    try:
        resume = session.execute(
            select(Resume).where(Resume.id == resume_id)
        ).scalar_one_or_none()

        if not resume:
            return {"error": "Resume not found", "jobs_scored": 0}

        if resume.status != "ready":
            return {"error": "Resume not ready", "jobs_scored": 0}

        relevant_clusters = get_relevant_clusters_sync(session)

        # F310 (data correctness): filter to ACTIVE job statuses only.
        # Pre-fix the rescore included every job in the relevant
        # cluster regardless of status, so EXPIRED + ARCHIVED rows
        # got scored too — wasted compute, and the user's "Top
        # Matches" panel could surface jobs they couldn't actually
        # apply to. Same active-statuses set that
        # ``maintenance_task.rescore_jobs`` and
        # ``reclassify_and_rescore`` use for their streaming loop.
        # Combined with F300's UPSERT-then-clean, the cleanup pass
        # also drops stale ResumeScore rows for jobs that flipped
        # to expired/archived since the last run.
        _ACTIVE_STATUSES = ("new", "under_review", "accepted")

        # Get all relevant + active jobs
        jobs = session.execute(
            select(Job)
            .where(
                Job.role_cluster.in_(relevant_clusters),
                Job.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(Job.relevance_score.desc())
        ).scalars().all()

        if not jobs:
            return {"error": "No relevant jobs found", "jobs_scored": 0}

        total = len(jobs)
        self.update_state(state="PROGRESS", meta={"current": 0, "total": total})

        # F300 (closes F105): switched from delete-old-then-rescore
        # to UPSERT-then-clean. Pre-fix every rescore deleted ALL
        # existing ResumeScore rows up-front, leaving the UI showing
        # ``jobs_scored=0`` for the full ~90s rescore window — the
        # user's "Best Score" / "Top Matches" panels went blank.
        # The new shape:
        #   1. Score each job → UPSERT (line 125 below already does
        #      this via ``on_conflict_do_update``).
        #   2. Track the set of job_ids we touched.
        #   3. After the loop, DELETE any ResumeScore rows for THIS
        #      resume whose job_id is NOT in the touched set —
        #      cleanup of jobs that were relevant in the old run
        #      but aren't anymore (e.g. expired, role-cluster
        #      changed).
        # Result: users see old scores (slightly stale) THROUGHOUT
        # the rescore window, gradually overwritten with new
        # numbers — no blank-screen UX. Worker crash mid-rescore
        # leaves the user with old-scores + partial-new-scores
        # which is strictly better than the pre-fix all-blank +
        # partial-new behaviour.
        scored_job_ids: set = set()

        # Load descriptions in bulk
        job_ids = [j.id for j in jobs]
        descriptions = {}
        if job_ids:
            desc_rows = session.execute(
                select(JobDescription).where(JobDescription.job_id.in_(job_ids))
            ).scalars().all()
            for d in desc_rows:
                descriptions[d.job_id] = d.text_content or ""

        # Score each job
        scored = 0
        fallback_used = 0
        for i, job in enumerate(jobs):
            desc_text = descriptions.get(job.id, "")

            # Regression finding 97: historical rows are missing a
            # JobDescription row entirely (the scan pipeline never wrote
            # them — fixed prospectively by the scan_task edit in this
            # commit). Until the next scan cycle re-upserts those rows,
            # fall back to extracting text straight from Job.raw_json so
            # resume scoring actually has something to keyword-match
            # against. Without this, the first run after deploy would
            # still produce the "4 distinct values across 600+ jobs"
            # failure mode. The fallback cost is one `extract_description`
            # call per empty row — same per-platform key mapping used by
            # the scan task, so results are consistent.
            if not desc_text:
                _, fallback_text = extract_description(
                    job.platform or "", job.raw_json or {}
                )
                if fallback_text:
                    desc_text = fallback_text
                    fallback_used += 1

            result = compute_ats_score(
                resume_text=resume.text_content,
                job_title=job.title,
                matched_role=job.matched_role or "",
                role_cluster=job.role_cluster or "",
                description_text=desc_text,
            )

            # Upsert on (resume_id, job_id). The delete-then-add above is
            # supposed to guarantee no existing row, but two concurrent
            # invocations of this task (post-upload + manual rescore +
            # `rescore_all_active_resumes` beat) race past the delete
            # and both try to insert the same (resume_id, job_id) pair
            # — that's how the table accumulated 10,414 rows for ~13k
            # jobs on test-admin. Once migration p6k7l8m9n0o1 lands the
            # UNIQUE index, plain `INSERT` would fail loudly with
            # IntegrityError and crash the whole rescore. ON CONFLICT
            # DO UPDATE is race-safe AND constraint-safe; the row
            # count stabilizes to exactly one per pair.
            stmt = pg_insert(ResumeScore.__table__).values(
                id=uuid.uuid4(),
                resume_id=resume.id,
                job_id=job.id,
                overall_score=result["overall_score"],
                keyword_score=result["keyword_score"],
                role_match_score=result["role_match_score"],
                format_score=result["format_score"],
                matched_keywords=result["matched_keywords"],
                missing_keywords=result["missing_keywords"],
                suggestions=result["suggestions"],
                scored_at=datetime.now(timezone.utc),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["resume_id", "job_id"],
                set_={
                    "overall_score": stmt.excluded.overall_score,
                    "keyword_score": stmt.excluded.keyword_score,
                    "role_match_score": stmt.excluded.role_match_score,
                    "format_score": stmt.excluded.format_score,
                    "matched_keywords": stmt.excluded.matched_keywords,
                    "missing_keywords": stmt.excluded.missing_keywords,
                    "suggestions": stmt.excluded.suggestions,
                    "scored_at": stmt.excluded.scored_at,
                },
            )
            session.execute(stmt)
            scored_job_ids.add(job.id)  # F300: track for final cleanup
            scored += 1

            # Update progress every 50 jobs
            if scored % 50 == 0:
                self.update_state(state="PROGRESS", meta={"current": scored, "total": total})
                session.flush()

        # F300 (closes F105): final cleanup pass — drop ResumeScore
        # rows for THIS resume whose job_id wasn't touched in the
        # current run. These are jobs that were relevant under the
        # old criteria but aren't anymore (expired, role_cluster
        # changed, etc.). Done as a single batched DELETE so the
        # transaction stays small.
        stale_deleted = 0
        if scored_job_ids:
            from sqlalchemy import delete as sa_delete
            stale_result = session.execute(
                sa_delete(ResumeScore).where(
                    ResumeScore.resume_id == resume.id,
                    ResumeScore.job_id.notin_(scored_job_ids),
                )
            )
            stale_deleted = stale_result.rowcount or 0

        session.commit()
        logger.info(
            "score_resume_task: resume=%s total=%d scored=%d "
            "raw_json_fallback=%d stale_pruned=%d",
            resume_id, total, scored, fallback_used, stale_deleted,
        )
        return {
            "jobs_scored": scored,
            "total": total,
            "raw_json_fallback": fallback_used,
            "stale_pruned": stale_deleted,
        }

    except Exception as exc:
        session.rollback()
        raise self.retry(exc=exc, countdown=10)
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.resume_score_task.rescore_all_active_resumes")
def rescore_all_active_resumes():
    """Fan out `score_resume_task` for every distinct active resume.

    Regression finding 96: the job-to-resume ATS scores were 11 days stale
    in prod — only 50.7% coverage of the relevant-jobs pool (2,642 / 5,206),
    all `scored_at` timestamps landing in a single 3-second window from the
    last manual rescore. Root cause was two missing hooks:
      (a) no beat schedule ever enqueued resume rescoring, and
      (b) the upload endpoint didn't enqueue `score_resume_task` on new
          uploads — users had to click a hidden button.
    This wrapper is the beat-schedule half; the upload trigger lives in
    `app/api/v1/resume.py::upload_resume`.

    We enqueue by `User.active_resume_id` rather than every `Resume` row
    because scoring is expensive (~90s for 5k jobs) and the UI only ever
    surfaces scores for the active persona. Idle / archived resumes stay
    cold until the user switches to them.

    Intentionally fires-and-forgets — each enqueued `score_resume_task`
    manages its own transaction and its own delete-and-replace semantics,
    so a partial fan-out still leaves the system in a valid state.
    """
    session = SyncSession()
    try:
        # DISTINCT because two users can't share an active resume (FK is
        # `User.active_resume_id -> Resume.id`, 1:many from resume's side)
        # but belt-and-suspenders against future schema changes that might
        # introduce sharing.
        active_ids = session.execute(
            select(User.active_resume_id)
            .where(
                User.active_resume_id.is_not(None),
                User.is_active.is_(True),
            )
            .distinct()
        ).scalars().all()

        enqueued = 0
        for rid in active_ids:
            if rid is None:
                continue
            score_resume_task.delay(str(rid))
            enqueued += 1

        logger.info(
            "rescore_all_active_resumes: enqueued %d resume(s) for rescoring",
            enqueued,
        )
        return {"enqueued": enqueued}

    finally:
        session.close()
