"""F404 — outreach to the people behind an application.

Tsenta's Networking tab picks the top hiring contacts at a company the
user applied to and drafts an email plus a LinkedIn note per contact.
This is the same feature with two differences the user asked for: the
platform never sends anything (the user copies the draft into Gmail,
Outlook or LinkedIn and presses Send themselves), and every claim in a
draft is checked against the résumé and Answer Book the way application
answers are (F396). Contacts are taken from the company_contacts table
the enrichment pipeline already fills; their email is verified on the
way in (``email_verification``) so the panel can say "valid", "likely"
or "unverified" instead of pretending.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Ranking: who is worth writing to, best first. Recruiters and talent
# partners answer candidates; engineering leads for the same cluster
# are the hiring managers; C-suite only at small companies.
_CATEGORY_RANK = {"hiring": 0, "talent": 0, "engineering_lead": 1, "executive": 2, "c_suite": 2, "other": 3}
_EMAIL_RANK = {"valid": 0, "catch_all": 1, "likely": 1, "unverified": 2, "unknown": 2, "": 3, "invalid": 4}
_SMALL_COMPANY = 200


def _employee_count(company) -> int | None:
    raw = str(getattr(company, "employee_count", "") or "")
    m = re.search(r"\d[\d,]*", raw)
    return int(m.group(0).replace(",", "")) if m else None


def rank_contacts(contacts: list, relevance: dict, company=None, limit: int = 3) -> list:
    """Order contacts for outreach and keep the top ``limit``.

    ``relevance`` maps contact id → JobContactRelevance score for this
    job (0 when none). A contact with neither a usable email nor a
    LinkedIn URL cannot be reached and is dropped; an ``invalid`` email
    counts as no email.
    """
    small = (_employee_count(company) or 10**9) <= _SMALL_COMPANY

    def reachable(c) -> bool:
        return bool((c.email and c.email_status != "invalid") or c.linkedin_url)

    def key(c):
        cat = _CATEGORY_RANK.get(c.role_category or "other", 3)
        if cat == 2 and not small:
            cat = 3  # a CEO of a 5,000-person company will not read it
        return (cat, -float(relevance.get(str(c.id), 0.0)), _EMAIL_RANK.get(c.email_status or "", 3) if c.email else 3,
                0 if c.linkedin_url else 1, -(c.confidence_score or 0.0))

    return sorted([c for c in contacts if reachable(c)], key=key)[:limit]


@dataclass
class OutreachDraft:
    email_subject: str = ""
    email_body: str = ""
    linkedin_note: str = ""
    enough_information: bool = True
    unsupported_claims: list[str] = field(default_factory=list)
    note: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {"email_subject": self.email_subject, "email_body": self.email_body, "linkedin_note": self.linkedin_note,
                "enough_information": self.enough_information, "unsupported_claims": list(self.unsupported_claims),
                "note": self.note, "error": self.error}


_DRAFT_SYSTEM = """You write a short, specific outreach message from a job candidate to one person at the company they just applied to. First person, the candidate's plain voice. The reader is busy: the email is 90 to 140 words, the LinkedIn connection note is at most 200 characters (LinkedIn's limit). Both must:
- say which role the candidate applied for, by title;
- give one or two concrete reasons the candidate fits, taken ONLY from the résumé or saved answers (an employer, a system they ran, a number, a certification). Never invent experience, employers, numbers, or mutual contacts;
- mention one specific thing about the company or the role from the job description, not generic praise;
- end with a small ask (a 15-minute call, or a look at the application), no pressure;
- read like a person wrote it: no "I hope this email finds you well", no "I am reaching out", no "passionate", no em dashes, no bullet points, no mention of AI or of the résumé as a document.
Address the reader by first name. Do not sign with a name you have to guess: sign with the candidate's name from the material, or omit the signature.
Reply ONLY with JSON: {"enough_information": true|false, "email_subject": "...", "email_body": "...", "linkedin_note": "..."}. If the résumé and saved answers give nothing concrete to say, set enough_information to false and leave the texts empty."""

_VERIFY_SYSTEM = """You are checking an outreach email and a LinkedIn note written on behalf of a job candidate against the candidate's material and the job description. List every factual claim that is NOT supported by the résumé, saved answers, job description or company facts (an employer, a duration, a tool, a project, a number, a certification, a mutual contact, a claim about the company). Opinions and intentions are fine. Reply ONLY with JSON: {"unsupported_claims": ["..."]} (empty list when everything is supported)."""

_REVISE_SYSTEM = """You are correcting an outreach email and LinkedIn note written on behalf of a job candidate. A fact-check found claims the material does not support. Rewrite both so that every remaining claim is supported by the résumé, saved answers, job description or company facts: remove unsupported claims or restate them only as far as the material supports. Keep the voice, the length band (email 90 to 140 words, note at most 200 characters), no em dashes, no mention of AI. Reply ONLY with JSON: {"email_subject": "...", "email_body": "...", "linkedin_note": "..."}."""


def _json_block(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end])


def _clean(s: str) -> str:
    return re.sub(r"\s*—\s*", ", ", str(s or "")).strip()


def _material(*, job_title, company, company_facts, job_description, resume_text, book, contact_name, contact_title) -> str:
    entries = "\n".join(f"- {e.get('question') or e.get('question_key')}: {e.get('answer')}"
                        for e in book if (e.get("answer") or "").strip())
    return (
        f"ROLE APPLIED FOR: {job_title} at {company}\n\nRECIPIENT: {contact_name}, {contact_title or 'title unknown'} at {company}\n\n"
        f"COMPANY FACTS:\n{company_facts or '(none)'}\n\nJOB DESCRIPTION (trimmed):\n{(job_description or '')[:6000]}\n\n"
        f"CANDIDATE RÉSUMÉ:\n{(resume_text or '')[:9000]}\n\nCANDIDATE'S SAVED ANSWERS:\n{entries[:3000] or '(none)'}"
    )


def draft_outreach(*, job_title, company, company_facts, job_description, resume_text, book, contact_name, contact_title,
                   client=None) -> OutreachDraft:
    from app.ai_client import AIRefused, AIUnavailable, complete

    material = _material(job_title=job_title, company=company, company_facts=company_facts, job_description=job_description,
                         resume_text=resume_text, book=book, contact_name=contact_name, contact_title=contact_title)
    try:
        text, _ = complete(material, system=_DRAFT_SYSTEM, answer_tokens=900, client=client)
        data = _json_block(text)
    except AIUnavailable:
        return OutreachDraft(error="AI is not configured")
    except AIRefused as exc:
        return OutreachDraft(error=f"the model declined: {exc.category or 'unspecified'}")
    except Exception as exc:
        logger.info("outreach: draft failed for %r: %s", contact_name, exc)
        return OutreachDraft(error="draft failed")
    if not data.get("enough_information") or not (data.get("email_body") or "").strip():
        return OutreachDraft(enough_information=False,
                             note="Your résumé and saved answers give nothing concrete to say to this person, so nothing was drafted.")
    d = OutreachDraft(email_subject=_clean(data.get("email_subject")), email_body=_clean(data.get("email_body")),
                      linkedin_note=_clean(data.get("linkedin_note"))[:200])
    try:
        vtext, _ = complete(f"{material}\n\nEMAIL TO CHECK:\nSubject: {d.email_subject}\n{d.email_body}\n\nLINKEDIN NOTE TO CHECK:\n{d.linkedin_note}",
                            system=_VERIFY_SYSTEM, answer_tokens=400, client=client)
        claims = [str(c).strip() for c in (_json_block(vtext).get("unsupported_claims") or []) if str(c).strip()]
    except Exception as exc:
        logger.info("outreach: verify failed for %r: %s", contact_name, exc)
        d.note = "Drafted from your résumé, the job description and the company. The fact-check pass failed, so read it carefully."
        return d
    if not claims:
        d.note = "Drafted from your résumé, the job description and the company. Every claim traced back to your material."
        return d
    try:
        rtext, _ = complete(f"{material}\n\nUNSUPPORTED CLAIMS:\n" + "\n".join(f"- {c}" for c in claims)
                            + f"\n\nEMAIL:\nSubject: {d.email_subject}\n{d.email_body}\n\nLINKEDIN NOTE:\n{d.linkedin_note}",
                            system=_REVISE_SYSTEM, answer_tokens=900, client=client)
        r = _json_block(rtext)
        d2 = OutreachDraft(email_subject=_clean(r.get("email_subject")) or d.email_subject, email_body=_clean(r.get("email_body")),
                           linkedin_note=_clean(r.get("linkedin_note"))[:200])
        vtext, _ = complete(f"{material}\n\nEMAIL TO CHECK:\nSubject: {d2.email_subject}\n{d2.email_body}\n\nLINKEDIN NOTE TO CHECK:\n{d2.linkedin_note}",
                            system=_VERIFY_SYSTEM, answer_tokens=400, client=client)
        claims2 = [str(c).strip() for c in (_json_block(vtext).get("unsupported_claims") or []) if str(c).strip()]
        if d2.email_body:
            d = d2
            claims = claims2
    except Exception as exc:
        logger.info("outreach: revise failed for %r: %s", contact_name, exc)
    d.unsupported_claims = claims
    d.note = ("Revised after a fact-check; every claim now traces back to your material." if not claims else
              "The fact-check still flags claims your material does not support. Check them before sending.")
    return d


def deep_links(to: str, subject: str, body: str) -> dict:
    from urllib.parse import quote

    if not to:
        return {}
    return {
        "mailto": f"mailto:{to}?subject={quote(subject)}&body={quote(body)}",
        "gmail": f"https://mail.google.com/mail/?view=cm&fs=1&to={quote(to)}&su={quote(subject)}&body={quote(body)}",
        "outlook": f"https://outlook.office.com/mail/deeplink/compose?to={quote(to)}&subject={quote(subject)}&body={quote(body)}",
    }
