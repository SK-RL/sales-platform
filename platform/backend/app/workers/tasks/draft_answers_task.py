"""F396 — draft answers for an application's free-text gaps, in the worker.

Enqueued by ``/applications/prepare`` (first view of Needs you) and by
``apply_task._halt`` (after a run stops). Drafts land in
``Application.platform_response["drafts"]`` keyed by field_key, and the
review page shows them in the gap's answer box. Never touches the form.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=0, acks_late=False, soft_time_limit=300, time_limit=360)
def draft_gap_answers_task(self, application_id: str) -> dict:
    from app.models.application import Application

    session = SyncSession()
    try:
        return _run(session, application_id)
    except Exception as exc:
        logger.exception("draft_gap_answers_task: failed for %s", application_id)
        try:
            session.rollback()
            row = session.get(Application, application_id)
            if row is not None:
                row.platform_response = {**(row.platform_response or {}), "drafts_error": f"{type(exc).__name__}: {exc}"[:300]}
                session.commit()
        except Exception:
            logger.info("draft_gap_answers_task: could not record the error", exc_info=True)
        return {"drafted": 0, "error": str(exc)[:200]}
    finally:
        session.close()


def _run(session, application_id: str) -> dict:
    from sqlalchemy import select

    from app.models.answer_book import AnswerBookEntry
    from app.models.application import Application
    from app.models.company import CompanyATSBoard
    from app.models.job import Job
    from app.models.resume import Resume
    from app.services.answer_drafts import draft_answer, draftable, employer_wants_own_words
    from app.services.question_service import get_or_fetch_questions_sync
    from app.workers.tasks._answer_prep import blocking_gaps, match_questions_to_answers

    if True:
        app_row = session.get(Application, application_id)
        if app_row is None:
            return {"drafted": 0, "reason": "no application"}
        job = session.get(Job, app_row.job_id)
        if job is None:
            return {"drafted": 0, "reason": "no job"}
        if job.resolved_job_id:
            job = session.get(Job, job.resolved_job_id) or job
        board = session.execute(select(CompanyATSBoard).where(CompanyATSBoard.company_id == job.company_id,
                                                              CompanyATSBoard.platform == job.platform,
                                                              CompanyATSBoard.is_active.is_(True))).scalars().first()
        questions = get_or_fetch_questions_sync(session, job, board.slug if board else "")
        entries = session.execute(select(AnswerBookEntry).where(
            AnswerBookEntry.user_id == app_row.user_id,
            (AnswerBookEntry.resume_id.is_(None)) | (AnswerBookEntry.resume_id == app_row.resume_id))).scalars().all()
        book = [{"question_key": e.question_key, "question": e.question, "answer": e.answer or "", "category": e.category or "", "source": e.source or "base"} for e in entries]
        matched = match_questions_to_answers(questions, book)
        resume = session.get(Resume, app_row.resume_id)
        satisfied = {"resume"} if getattr(resume, "file_data", None) else set()
        gaps = {g["field_key"] for g in blocking_gaps(matched, satisfied_field_keys=satisfied)}
        pr = dict(app_row.platform_response or {})
        drafts = dict(pr.get("drafts") or {})
        own_words = [m for m in matched if m["field_key"] in gaps and m.get("field_type") in ("text", "textarea")
                     and employer_wants_own_words(m.get("label", ""), m.get("description", ""))]
        for m in own_words:
            drafts.setdefault(m["field_key"], {"text": "", "enough_information": False, "unsupported_claims": [], "error": "",
                                               "note": "This employer asks for your own words, so nothing was drafted. Write it yourself.",
                                               "label": m["label"], "drafted_at": datetime.now(timezone.utc).isoformat()})
        targets = [m for m in matched if m["field_key"] in gaps and draftable(m)]
        if not targets:
            if own_words:
                pr["drafts"] = drafts
                app_row.platform_response = pr
                session.commit()
            pr["drafts_run"] = {"at": datetime.now(timezone.utc).isoformat(), "drafted": 0, "reason": "no draftable gaps",
                                "gaps": sorted(gaps)}
            app_row.platform_response = pr
            session.commit()
            return {"drafted": 0, "reason": "no draftable gaps"}
        company = getattr(getattr(job, "company", None), "name", "") or ""
        # F405 — a draft without the job description is a résumé dump.
        from app.services.job_description_service import ensure_description_sync

        jd, jd_source = ensure_description_sync(session, job, board.slug if board else "")
        n = 0
        for m in targets:
            if m["field_key"] in drafts and drafts[m["field_key"]].get("text"):
                continue  # keep an existing draft (the user may be editing it)
            d = draft_answer(question=m["label"], description=m.get("description") or "", job_title=job.title, company=company,
                             job_description=jd, resume_text=getattr(resume, "text_content", "") or "", book=book)
            drafts[m["field_key"]] = {**d.as_dict(), "label": m["label"], "drafted_at": datetime.now(timezone.utc).isoformat(),
                                      "jd_source": jd_source}
            n += 1
        pr["drafts"] = drafts
        app_row.platform_response = pr
        session.commit()
        logger.info("draft_gap_answers_task: %s drafts for application %s", n, application_id)
        # F401 — always leave a trace on the row, even when nothing was
        # drafted, so "why is there no draft?" is answerable from the API.
        pr["drafts_run"] = {"at": datetime.now(timezone.utc).isoformat(), "drafted": n, "targets": [m["field_key"] for m in targets],
                            "jd_source": jd_source, "jd_words": len(jd.split())}
        app_row.platform_response = pr
        session.commit()
        return {"drafted": n}
