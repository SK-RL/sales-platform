"""Company enrichment Celery tasks."""

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, or_, func

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession
from app.models.company import Company
from app.config import get_settings

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks.enrichment_task.enrich_company", bind=True, max_retries=2)
def enrich_company(self, company_id: str):
    """Enrich a company record using the self-contained enrichment engine."""
    session = SyncSession()
    try:
        company = session.execute(
            select(Company).where(Company.id == company_id)
        ).scalar_one_or_none()
        if not company:
            logger.warning("Company not found: %s", company_id)
            return {"company_id": company_id, "status": "not_found"}

        company.enrichment_status = "enriching"
        session.commit()

        from app.services.enrichment.orchestrator import run_enrichment
        result = run_enrichment(session, company)

        # Orchestrator already commits final status — just re-read for logging
        session.refresh(company)
        logger.info("Enriched company %s: %s", company.name, result)
        return {"company_id": company_id, "status": company.enrichment_status, **result}

    except Exception as exc:
        logger.exception("enrich_company failed for %s: %s", company_id, exc)
        try:
            company = session.execute(
                select(Company).where(Company.id == company_id)
            ).scalar_one_or_none()
            if company:
                company.enrichment_status = "failed"
                company.enrichment_error = str(exc)[:500]
                session.commit()
        except Exception:
            session.rollback()
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.enrichment_task.enrich_target_companies_batch")
def enrich_target_companies_batch():
    """Nightly: enrich companies that are stale or never enriched.

    Historically this filtered on ``Company.is_target.is_(True)`` — but
    only a curated handful of companies ever make it to `is_target=True`
    (via `auto_target_companies` or admin action), so the long tail of
    companies ingested from ATS scans was NEVER enriched. Users reported
    many companies with empty description/funding/logo fields.

    New eligibility: any company that (a) has at least one active ATS
    board (so enriching it actually has a signal to attach to), (b) is
    never-enriched OR past the stale cutoff, (c) is not currently being
    enriched by a parallel task. Ordered by `is_target DESC` first
    (curated companies keep priority), then by `created_at DESC` so
    fresh arrivals get enriched before the DB's oldest entries. Capped
    at `enrichment_batch_size` (50/hour default) so external API quota
    and scraper bandwidth stay bounded — the full 786-company corpus
    converges in ~16 hours.

    Task name is retained (not renamed) so celery beat keeps picking it
    up and any admin-triggered `.delay()` calls from API endpoints that
    reference the name keep working.
    """
    session = SyncSession()
    try:
        settings = get_settings()
        stale_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.enrichment_stale_days)

        # Subquery: companies that have at least one active board. LEFT
        # JOIN would work too, but EXISTS is cheaper when we only need
        # the boolean — Postgres can short-circuit after the first hit.
        from app.models.company import CompanyATSBoard
        from sqlalchemy import exists
        has_active_board = exists().where(
            CompanyATSBoard.company_id == Company.id,
            CompanyATSBoard.is_active.is_(True),
        )

        companies = session.execute(
            select(Company).where(
                has_active_board,
                or_(
                    Company.enriched_at.is_(None),
                    Company.enriched_at < stale_cutoff,
                ),
                Company.enrichment_status != "enriching",
            )
            .order_by(Company.is_target.desc(), Company.created_at.desc())
            .limit(settings.enrichment_batch_size)
        ).scalars().all()

        queued = 0
        for company in companies:
            enrich_company.delay(str(company.id))
            queued += 1

        logger.info(
            "Queued %d companies for enrichment (is_target priority, batch cap=%d)",
            queued, settings.enrichment_batch_size,
        )
        return {"queued": queued}
    except Exception as exc:
        logger.exception("enrich_target_companies_batch failed: %s", exc)
        raise
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.enrichment_task.verify_stale_emails")
def verify_stale_emails():
    """Re-verify contact emails that are older than the verification threshold."""
    from app.models.company_contact import CompanyContact

    session = SyncSession()
    try:
        settings = get_settings()
        stale_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.contact_verify_stale_days)

        contacts = session.execute(
            select(CompanyContact).where(
                CompanyContact.email != "",
                CompanyContact.email_status.in_(["unverified", "valid"]),
                or_(
                    CompanyContact.email_verified_at.is_(None),
                    CompanyContact.email_verified_at < stale_cutoff,
                ),
            ).limit(100)
        ).scalars().all()

        verified = 0
        for contact in contacts:
            try:
                from app.services.enrichment.email_verifier import verify_email_smtp
                result = verify_email_smtp(contact.email)
                contact.email_status = result.get("status", "unknown")
                contact.email_verified_at = datetime.now(timezone.utc)
                contact.last_verified_at = datetime.now(timezone.utc)
                verified += 1
            except Exception as e:
                logger.warning("Email verification failed for %s: %s", contact.email, e)

        session.commit()
        logger.info("Verified %d stale emails", verified)
        return {"verified": verified}
    except Exception as exc:
        logger.exception("verify_stale_emails failed: %s", exc)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.enrichment_task.deduplicate_contacts")
def deduplicate_contacts():
    """
    Find and merge duplicate contacts within the same company.
    Duplicates are identified by: same company_id + same normalized name.
    The record with higher confidence_score (or more data) is kept; duplicates are deleted.
    """
    from app.models.company_contact import CompanyContact

    session = SyncSession()
    merged = 0
    try:
        # Find groups with same company + same lowercased full name
        dup_groups = session.execute(
            select(
                CompanyContact.company_id,
                func.lower(func.trim(CompanyContact.first_name)).label("fn"),
                func.lower(func.trim(CompanyContact.last_name)).label("ln"),
                func.count(CompanyContact.id).label("cnt"),
            )
            .where(CompanyContact.first_name != "", CompanyContact.last_name != "")
            .group_by(
                CompanyContact.company_id,
                func.lower(func.trim(CompanyContact.first_name)),
                func.lower(func.trim(CompanyContact.last_name)),
            )
            .having(func.count(CompanyContact.id) > 1)
        ).all()

        for row in dup_groups:
            dupes = session.execute(
                select(CompanyContact).where(
                    CompanyContact.company_id == row.company_id,
                    func.lower(func.trim(CompanyContact.first_name)) == row.fn,
                    func.lower(func.trim(CompanyContact.last_name)) == row.ln,
                ).order_by(CompanyContact.confidence_score.desc(), CompanyContact.created_at.asc())
            ).scalars().all()

            if len(dupes) < 2:
                continue

            # Keep the first (highest confidence / earliest created)
            keeper = dupes[0]
            for dup in dupes[1:]:
                # Merge data into keeper — fill any empty fields
                if not keeper.email and dup.email:
                    keeper.email = dup.email
                    keeper.email_status = dup.email_status
                if not keeper.phone and dup.phone:
                    keeper.phone = dup.phone
                if not keeper.linkedin_url and dup.linkedin_url:
                    keeper.linkedin_url = dup.linkedin_url
                if not keeper.title and dup.title:
                    keeper.title = dup.title
                if dup.is_decision_maker:
                    keeper.is_decision_maker = True
                if dup.confidence_score > keeper.confidence_score:
                    keeper.confidence_score = dup.confidence_score
                session.delete(dup)
                merged += 1

        session.commit()
        logger.info("Deduplication removed %d duplicate contacts", merged)
        return {"merged": merged}
    except Exception as exc:
        logger.exception("deduplicate_contacts failed: %s", exc)
        session.rollback()
        raise
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────
# F354 — recruiter/HR contact harvesting (sheet columns + JD text)
# ─────────────────────────────────────────────────────────────────────

import re as _re
import uuid as _uuid

_EMAIL_RE = _re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Local-part tokens that mark a hiring/application mailbox. These are
# accepted from ANY domain (an agency gmail published in a JD is still
# an intentional application address).
_HIRING_LOCAL_TOKENS = (
    "career", "job", "talent", "recruit", "hr", "hiring",
    "apply", "application", "people", "resume", "cv",
)

# Domains that appear inside JD boilerplate but are never a contact:
# ATS infrastructure, trackers, social, image CDNs.
_JUNK_EMAIL_DOMAINS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "bamboohr.com", "myworkdayjobs.com", "smartrecruiters.com",
    "jobvite.com", "recruitee.com", "linkedin.com", "example.com",
    "sentry.io", "wixpress.com", "2x.png", "email.com",
)


def _looks_like_hiring_mailbox(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return any(tok in local for tok in _HIRING_LOCAL_TOKENS)


def _company_domain(company: Company) -> str:
    dom = (company.domain or "").strip().lower()
    if dom:
        return dom
    website = (company.website or "").strip().lower()
    m = _re.search(r"https?://(?:www\.)?([^/]+)", website)
    return m.group(1) if m else ""


def _upsert_contact(
    session, company_id, *, email: str, first_name: str = "",
    last_name: str = "", title: str = "", linkedin: str = "",
    role_category: str = "talent", source: str = "",
    confidence: float = 0.5,
) -> bool:
    """Insert a contact unless one with the same email already exists
    for the company (matches the F282 partial-unique semantics:
    case-insensitive, trimmed). Returns True when inserted."""
    from app.models.company_contact import CompanyContact

    email_norm = email.strip().lower()
    if not email_norm:
        return False
    existing = session.execute(
        select(CompanyContact.id).where(
            CompanyContact.company_id == company_id,
            func.lower(func.trim(CompanyContact.email)) == email_norm,
        )
    ).scalar_one_or_none()
    if existing:
        return False
    session.add(CompanyContact(
        id=_uuid.uuid4(),
        company_id=company_id,
        first_name=first_name[:200],
        last_name=last_name[:200],
        title=title[:300],
        role_category=role_category,
        email=email.strip()[:300],
        linkedin_url=linkedin[:500],
        source=source,
        confidence_score=confidence,
    ))
    return True


@celery_app.task(name="app.workers.tasks.enrichment_task.sync_sheet_contacts", bind=True, max_retries=1)
def sync_sheet_contacts(self):
    """F354a — promote recruiter POCs from team Google-Sheet rows into
    CompanyContact.

    The team's sheets carry hand-curated contact columns (Name /
    Designation / Email id / LinkedIn / Company Email) that the
    fetcher captures into ``raw_json.row``. Nightly, walk every
    google_sheet job and upsert those contacts onto the job's
    (already correctly-resolved, F353) company:

      * ``contact_email`` → a named person, role_category="talent",
        confidence 0.9 (team-curated beats every scraped source).
      * ``company_email`` → the employer's hiring mailbox,
        role_category="hiring", confidence 0.8.

    Idempotent via the (company_id, lower(email)) existence check —
    re-runs and unchanged sheet rows are no-ops.
    """
    from app.models.job import Job

    session = SyncSession()
    added = 0
    try:
        rows = session.execute(
            select(Job.company_id, Job.raw_json).where(
                Job.platform == "google_sheet"
            )
        ).all()
        for company_id, raw_json in rows:
            row = (raw_json or {}).get("row") or {}
            contact_email = (row.get("contact_email") or "").strip()
            company_email = (row.get("company_email") or "").strip()
            name = (row.get("contact_name") or "").strip()
            first, _, last = name.partition(" ")
            if contact_email and _EMAIL_RE.fullmatch(contact_email):
                if _upsert_contact(
                    session, company_id,
                    email=contact_email,
                    first_name=first, last_name=last,
                    title=(row.get("contact_title") or "").strip(),
                    linkedin=(row.get("contact_linkedin") or "").strip(),
                    role_category="talent",
                    source="google_sheet",
                    confidence=0.9,
                ):
                    added += 1
            if company_email and _EMAIL_RE.fullmatch(company_email):
                if _upsert_contact(
                    session, company_id,
                    email=company_email,
                    role_category="hiring",
                    source="google_sheet",
                    confidence=0.8,
                ):
                    added += 1
        session.commit()
        logger.info("sync_sheet_contacts: %d contacts added from %d sheet rows", added, len(rows))
        return {"contacts_added": added, "sheet_rows": len(rows)}
    except Exception as e:
        logger.exception("sync_sheet_contacts failed: %s", e)
        session.rollback()
        raise self.retry(exc=e, countdown=300)
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.enrichment_task.mine_jd_contact_emails", bind=True, max_retries=1)
def mine_jd_contact_emails(self, batch_size: int = 5000):
    """F354b — mine employer-published emails out of stored JDs.

    235k+ job descriptions sit in the DB; many contain application /
    recruiter addresses the employer published precisely to be
    contacted ("send your CV to talent@acme.com"). Nightly, scan a
    batch (SQL-prefiltered to rows containing '@') and upsert:

      * hiring-keyword local parts (careers@/talent@/recruit@/hr@/…)
        → role_category="hiring", any domain except ATS/tracker junk,
        confidence 0.7;
      * other emails → accepted ONLY when the domain matches the
        job's company domain (person address on the employer's own
        domain) → role_category="talent", confidence 0.6. Everything
        else is discarded — that rule is what keeps random JD noise
        (partner logos, support addresses of tools) out.

    Batch is ordered by newest-first so fresh postings are mined the
    night they arrive; the idempotent upsert makes overlap across
    nights free.
    """
    from app.models.job import Job, JobDescription

    session = SyncSession()
    added = scanned = 0
    try:
        rows = session.execute(
            select(
                JobDescription.text_content, Job.company_id, Job.id
            )
            .join(Job, Job.id == JobDescription.job_id)
            .where(JobDescription.text_content.like("%@%"))
            .order_by(JobDescription.fetched_at.desc())
            .limit(batch_size)
        ).all()

        # Company-domain cache for the person-email rule.
        domain_cache: dict = {}
        for text, company_id, _job_id in rows:
            scanned += 1
            for email in set(_EMAIL_RE.findall(text or "")[:20]):
                domain = email.split("@", 1)[1].lower()
                if any(domain.endswith(j) for j in _JUNK_EMAIL_DOMAINS):
                    continue
                if _looks_like_hiring_mailbox(email):
                    role, conf = "hiring", 0.7
                else:
                    if company_id not in domain_cache:
                        comp = session.get(Company, company_id)
                        domain_cache[company_id] = _company_domain(comp) if comp else ""
                    cdom = domain_cache[company_id]
                    if not cdom or not domain.endswith(cdom):
                        continue
                    role, conf = "talent", 0.6
                if _upsert_contact(
                    session, company_id,
                    email=email, role_category=role,
                    source="jd_text", confidence=conf,
                ):
                    added += 1
        session.commit()
        logger.info(
            "mine_jd_contact_emails: %d contacts from %d descriptions",
            added, scanned,
        )
        return {"contacts_added": added, "descriptions_scanned": scanned}
    except Exception as e:
        logger.exception("mine_jd_contact_emails failed: %s", e)
        session.rollback()
        raise self.retry(exc=e, countdown=300)
    finally:
        session.close()
