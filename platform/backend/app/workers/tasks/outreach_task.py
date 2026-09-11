"""F404 — build the outreach bundle for one application, in the worker.

Picks the top three reachable contacts at the company (``rank_contacts``),
verifies their email (``email_verification``), drafts an email and a
LinkedIn note per contact (``outreach.draft_outreach``, Opus 5 with a
fact-check) and stores the bundle in
``Application.platform_response["outreach"]``. Nothing is sent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=0, acks_late=False, soft_time_limit=420, time_limit=480)
def draft_outreach_task(self, application_id: str, contact_ids: list[str] | None = None) -> dict:
    from app.models.application import Application

    session = SyncSession()
    try:
        return _run(session, application_id, contact_ids)
    except Exception as exc:
        logger.exception("draft_outreach_task: failed for %s", application_id)
        try:
            session.rollback()
            row = session.get(Application, application_id)
            if row is not None:
                pr = dict(row.platform_response or {})
                pr["outreach"] = {**(pr.get("outreach") or {}), "running": False,
                                  "error": f"{type(exc).__name__}: {exc}"[:300]}
                row.platform_response = pr
                session.commit()
        except Exception:
            logger.info("draft_outreach_task: could not record the error", exc_info=True)
        return {"drafted": 0, "error": str(exc)[:200]}
    finally:
        session.close()


def _company_facts(company) -> str:
    bits = []
    for label, attr in (("Industry", "industry"), ("Size", "employee_count"), ("Headquarters", "headquarters"),
                        ("Funding stage", "funding_stage"), ("Website", "website")):
        v = getattr(company, attr, "") or ""
        if v:
            bits.append(f"{label}: {v}")
    desc = (getattr(company, "description", "") or "").strip()
    if desc:
        bits.append(f"About: {desc[:1200]}")
    return "\n".join(bits)


def _run(session, application_id: str, contact_ids: list[str] | None) -> dict:
    from sqlalchemy import select

    from app.models.answer_book import AnswerBookEntry
    from app.models.application import Application
    from app.models.company import Company
    from app.models.company_contact import CompanyContact, JobContactRelevance
    from app.models.job import Job
    from app.models.resume import Resume
    from app.services.enrichment.email_verification import observed_emails_for_domain, verify_email
    from app.services.outreach import deep_links, draft_outreach, rank_contacts

    app_row = session.get(Application, application_id)
    if app_row is None:
        return {"drafted": 0, "reason": "no application"}
    job = session.get(Job, app_row.job_id)
    if job is None:
        return {"drafted": 0, "reason": "no job"}
    if job.resolved_job_id:
        job = session.get(Job, job.resolved_job_id) or job
    company = session.get(Company, job.company_id)
    contacts = session.execute(select(CompanyContact).where(CompanyContact.company_id == job.company_id)).scalars().all()
    relevance = {str(r.contact_id): float(r.relevance_score or 0.0) for r in session.execute(
        select(JobContactRelevance).where(JobContactRelevance.job_id == job.id)).scalars().all()}
    pr = dict(app_row.platform_response or {})
    bundle = dict(pr.get("outreach") or {})
    existing = {c["contact_id"]: c for c in (bundle.get("contacts") or [])}

    if contact_ids:
        chosen = [c for c in contacts if str(c.id) in set(contact_ids)]
    else:
        chosen = rank_contacts(contacts, relevance, company, limit=3)
    if not chosen:
        pr["outreach"] = {**bundle, "running": False, "contacts": [], "at": datetime.now(timezone.utc).isoformat(),
                          "reason": "No reachable contact at this company yet (no email or LinkedIn on file)."}
        app_row.platform_response = pr
        session.commit()
        return {"drafted": 0, "reason": "no contacts"}

    # Verify emails first, so the draft is not addressed to a dead mailbox.
    for c in chosen:
        if c.email and c.email_status in ("unverified", "unknown", "likely", ""):
            domain = c.email.rsplit("@", 1)[1].lower() if "@" in c.email else ""
            v = verify_email(c.email, observed_emails_for_domain(session, domain))
            c.email_status = v.status
            c.email_verified_at = datetime.now(timezone.utc)
            c.last_verified_at = datetime.now(timezone.utc)
            c.outreach_note = (c.outreach_note or "")
            bundle.setdefault("verification", {})[str(c.id)] = v.as_dict()
    session.commit()

    entries = session.execute(select(AnswerBookEntry).where(
        AnswerBookEntry.user_id == app_row.user_id,
        (AnswerBookEntry.resume_id.is_(None)) | (AnswerBookEntry.resume_id == app_row.resume_id))).scalars().all()
    book = [{"question_key": e.question_key, "question": e.question, "answer": e.answer or ""} for e in entries]
    resume = session.get(Resume, app_row.resume_id)
    resume_text = getattr(resume, "text_content", "") or ""
    jd = getattr(getattr(job, "description", None), "text_content", "") or ""
    company_name = getattr(company, "name", "") or ""
    facts = _company_facts(company) if company else ""

    out, n = [], 0
    for c in chosen:
        name = f"{c.first_name} {c.last_name}".strip() or "there"
        prev = existing.get(str(c.id))
        if prev and prev.get("draft", {}).get("email_body") and not contact_ids:
            d = prev["draft"]  # keep a draft the user may have edited
        else:
            d = draft_outreach(job_title=job.title, company=company_name, company_facts=facts, job_description=jd,
                               resume_text=resume_text, book=book, contact_name=name, contact_title=c.title or "").as_dict()
            n += 1
        out.append({
            "contact_id": str(c.id), "name": name, "title": c.title or "", "role_category": c.role_category or "",
            "email": c.email or "", "email_status": c.email_status or "", "linkedin_url": c.linkedin_url or "",
            "verification": (bundle.get("verification") or {}).get(str(c.id)),
            "relevance": relevance.get(str(c.id), 0.0),
            "outreach_status": c.outreach_status or "not_contacted",
            "last_outreach_at": c.last_outreach_at.isoformat() if c.last_outreach_at else None,
            "draft": d,
            "links": deep_links(c.email if c.email_status != "invalid" else "", d.get("email_subject") or "", d.get("email_body") or ""),
            "sent": (prev or {}).get("sent") or [],
        })
    pr["outreach"] = {**bundle, "running": False, "error": "", "contacts": out, "at": datetime.now(timezone.utc).isoformat(),
                      "drafted": n}
    app_row.platform_response = pr
    session.commit()
    logger.info("draft_outreach_task: %s drafts for application %s", n, application_id)
    return {"drafted": n, "contacts": len(out)}


@celery_app.task(bind=True, max_retries=0, acks_late=False, soft_time_limit=540, time_limit=600)
def verify_company_contacts_task(self, company_id: str, limit: int = 60) -> dict:
    """Verify the unsettled emails of one company's contacts (F404)."""
    from sqlalchemy import select

    from app.models.company_contact import CompanyContact
    from app.services.enrichment.email_verification import observed_emails_for_domain, verify_email

    session = SyncSession()
    try:
        rows = session.execute(select(CompanyContact).where(
            CompanyContact.company_id == company_id, CompanyContact.email != "",
            CompanyContact.email_status.in_(["unverified", "unknown", "likely", ""]),
        ).order_by(CompanyContact.confidence_score.desc()).limit(limit)).scalars().all()
        counts: dict[str, int] = {}
        for c in rows:
            domain = c.email.rsplit("@", 1)[1].lower() if "@" in c.email else ""
            v = verify_email(c.email, observed_emails_for_domain(session, domain))
            c.email_status = v.status
            c.email_verified_at = datetime.now(timezone.utc)
            c.last_verified_at = datetime.now(timezone.utc)
            counts[v.status] = counts.get(v.status, 0) + 1
            session.commit()
        return {"verified": len(rows), "by_status": counts}
    except Exception as exc:
        logger.exception("verify_company_contacts_task: failed for %s", company_id)
        session.rollback()
        return {"verified": 0, "error": str(exc)[:200]}
    finally:
        session.close()
