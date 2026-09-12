"""F406 — project-idea research for one application, and the corpus-wide
generic ideas, in the worker. Stage 1 only: ideas, never code."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession

logger = logging.getLogger(__name__)

GENERIC_KEY = "proof:generic:{user_id}"
EVAL_KEY = "proof:eval:{user_id}"
_TTL = 14 * 24 * 3600


def _redis():
    import redis

    from app.config import get_settings

    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=5)


@celery_app.task(bind=True, max_retries=0, acks_late=False, soft_time_limit=420, time_limit=480)
def project_ideas_task(self, application_id: str) -> dict:
    from app.models.application import Application

    session = SyncSession()
    try:
        return _run_one(session, application_id)
    except Exception as exc:
        logger.exception("project_ideas_task: failed for %s", application_id)
        try:
            session.rollback()
            row = session.get(Application, application_id)
            if row is not None:
                pr = dict(row.platform_response or {})
                pr["project_ideas"] = {**(pr.get("project_ideas") or {}), "running": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
                row.platform_response = pr
                session.commit()
        except Exception:
            logger.info("project_ideas_task: could not record the error", exc_info=True)
        return {"ideas": 0, "error": str(exc)[:200]}
    finally:
        session.close()


def _run_one(session, application_id: str) -> dict:
    from sqlalchemy import select

    from app.models.application import Application
    from app.models.company import Company, CompanyATSBoard
    from app.models.job import Job
    from app.models.resume import Resume
    from app.services.job_description_service import ensure_description_sync
    from app.services.proof_project import gather_research, generate_ideas, ground_ideas

    t0 = time.monotonic()
    app_row = session.get(Application, application_id)
    if app_row is None:
        return {"ideas": 0, "reason": "no application"}
    job = session.get(Job, app_row.job_id)
    if job is None:
        return {"ideas": 0, "reason": "no job"}
    if job.resolved_job_id:
        job = session.get(Job, job.resolved_job_id) or job
    company = session.get(Company, job.company_id)
    board = session.execute(select(CompanyATSBoard).where(CompanyATSBoard.company_id == job.company_id,
                                                          CompanyATSBoard.platform == job.platform,
                                                          CompanyATSBoard.is_active.is_(True))).scalars().first()
    jd, _ = ensure_description_sync(session, job, board.slug if board else "")
    research = gather_research(session, job, company, job_description=jd)
    resume = session.get(Resume, app_row.resume_id)
    result = ground_ideas(generate_ideas(research, getattr(resume, "text_content", "") or ""), research)
    pr = dict(app_row.platform_response or {})
    prev = dict(pr.get("project_ideas") or {})
    pr["project_ideas"] = {
        "running": False, "error": result.get("error") or "", "at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - t0, 1),
        "research": {"found": research["found"], "jd_terms": research["jd_terms"],
                     "sources": [{"id": s["id"], "kind": s["kind"], "title": s["title"], "url": s.get("url", ""), "chars": len(s["text"])}
                                 for s in research["sources"]]},
        "research_verdict": result.get("research_verdict", ""),
        "ideas": result.get("ideas") or [],
        "chosen": prev.get("chosen"),
    }
    app_row.platform_response = pr
    session.commit()
    logger.info("project_ideas_task: %s ideas for application %s (%s)", len(result.get("ideas") or []), application_id,
                ",".join(k for k, v in research["found"].items() if v is True) or "no external sources")
    return {"ideas": len(result.get("ideas") or []), "specificity": [i["specificity"] for i in result.get("ideas") or []]}


@celery_app.task(bind=True, max_retries=0, acks_late=False, soft_time_limit=600, time_limit=660)
def generic_project_ideas_task(self, user_id: str, resume_id: str | None = None) -> dict:
    from sqlalchemy import select

    from app.models.resume import Resume
    from app.services.proof_project import corpus_term_coverage, generate_generic_ideas

    session = SyncSession()
    try:
        resume = session.get(Resume, resume_id) if resume_id else session.execute(
            select(Resume).where(Resume.user_id == user_id).order_by(Resume.uploaded_at.desc())).scalars().first()
        corpus = corpus_term_coverage(session)
        result = generate_generic_ideas(corpus, getattr(resume, "text_content", "") or "")
        payload = {**result, "coverage": corpus["coverage"], "resume_id": str(getattr(resume, "id", "") or ""), "running": False}
        _redis().set(GENERIC_KEY.format(user_id=user_id), json.dumps(payload), ex=_TTL)
        return {"ideas": len(result.get("ideas") or []), "jobs": corpus["jobs"]}
    except Exception as exc:
        logger.exception("generic_project_ideas_task: failed for %s", user_id)
        try:
            _redis().set(GENERIC_KEY.format(user_id=user_id), json.dumps({"ideas": [], "running": False, "error": str(exc)[:300]}), ex=_TTL)
        except Exception:
            pass
        return {"ideas": 0, "error": str(exc)[:200]}
    finally:
        session.close()


@celery_app.task(bind=True, max_retries=0, acks_late=False, soft_time_limit=3600, time_limit=3660)
def project_ideas_eval_task(self, user_id: str, application_ids: list[str]) -> dict:
    """Stage-1 test: run the idea pipeline over many applications and
    keep a compact report so the ideas can be judged before any code is
    written for them."""
    from app.models.application import Application

    r = _redis()
    key = EVAL_KEY.format(user_id=user_id)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "running": True, "total": len(application_ids), "rows": []}
    r.set(key, json.dumps(report), ex=_TTL)
    for app_id in application_ids:
        session = SyncSession()
        try:
            t0 = time.monotonic()
            out = _run_one(session, app_id)
            row = session.get(Application, app_id)
            pi = (row.platform_response or {}).get("project_ideas") or {} if row else {}
            job_title = company = ""
            if row is not None:
                from app.models.company import Company
                from app.models.job import Job

                job = session.get(Job, row.job_id)
                job_title = job.title if job else ""
                co = session.get(Company, job.company_id) if job else None
                company = getattr(co, "name", "") or ""
            report["rows"].append({
                "application_id": app_id, "job_title": job_title, "company": company, "seconds": round(time.monotonic() - t0, 1),
                "found": (pi.get("research") or {}).get("found"), "sources": [s["id"] for s in (pi.get("research") or {}).get("sources", [])],
                "verdict": pi.get("research_verdict", ""), "error": pi.get("error", ""),
                "ideas": [{"title": i.get("title"), "angle": i.get("angle"), "specificity": i.get("specificity"),
                           "grounded": f"{i.get('grounded_evidence')}/{i.get('total_evidence')}", "effort_hours": i.get("effort_hours"),
                           "recipient_role": i.get("recipient_role"), "summary": i.get("summary")} for i in pi.get("ideas") or []],
                "result": out,
            })
        except Exception as exc:
            report["rows"].append({"application_id": app_id, "error": str(exc)[:300]})
        finally:
            session.close()
        r.set(key, json.dumps(report), ex=_TTL)
    report["running"] = False
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    r.set(key, json.dumps(report), ex=_TTL)
    return {"rows": len(report["rows"])}
