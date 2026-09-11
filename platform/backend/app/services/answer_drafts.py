"""Draft a role-specific free-text answer for the user to approve.

F396. The Answer Book covers questions with one stable answer. It cannot
cover "Explain your Cloud Inference experience in 3-4 lines" or "Why are
you a great fit for this role" — those were a quarter of the required
questions on the batch-2 forms, and every one stopped in Needs you with
nothing to go on. This drafts an answer from the user's résumé, Answer
Book and the job description, then a second pass checks every claim in
the draft against those sources and strips what isn't supported.

Nothing here is ever sent on its own: the draft is shown in Needs you
with a "Use this" box; only what the user saves goes into the Answer
Book and from there into the form. Legal, compensation and protected-
class questions are never drafted (``is_never_infer_field``).

Model: ``claude-opus-5`` via ``app.ai_client.complete`` (Sarthak, 2026-
09-11: "use opus 5 for everything"). Two short calls at medium effort;
a few cents per question.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_DRAFT_SYSTEM = """You write short answers to job-application questions on behalf of a candidate, in the first person, in the candidate's own plain voice.

Rules that override everything else:
- Use ONLY facts in the candidate's résumé and saved answers. Never invent employers, dates, tools, projects, numbers, certifications or outcomes. Do not embellish.
- If the material does not cover what the question asks, set "enough_information" to false and leave "answer" empty. Do not write a vague filler answer.
- Match the length the question asks for; otherwise 2-5 sentences. No headings, no bullet lists unless the question asks for a list, no sign-off.
- Do not mention that you are an AI, do not mention "the résumé", and do not address the recruiter by name.
- Do not use em dashes.

Respond with JSON only: {"enough_information": true|false, "answer": "...", "facts_used": ["short fact from the material", ...]}"""

_VERIFY_SYSTEM = """You are checking a job-application answer against the candidate's material. List every factual claim in the answer that is NOT supported by the résumé or saved answers (an employer, a duration, a tool, a project, a number, a certification, an outcome). Paraphrase and reasonable framing of supported facts are fine. Opinions and intentions ("I'm excited to...") are fine.

Respond with JSON only: {"unsupported_claims": ["...", ...]}"""


@dataclass
class Draft:
    text: str = ""
    enough_information: bool = False
    unsupported_claims: list[str] = field(default_factory=list)
    note: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "enough_information": self.enough_information,
            "unsupported_claims": list(self.unsupported_claims),
            "note": self.note,
            "error": self.error,
        }


def _json_block(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end])


def _material(resume_text: str, book: list[dict], job_title: str, company: str, job_description: str) -> str:
    entries = "\n".join(f"- {e.get('question') or e.get('question_key')}: {e.get('answer')}"
                        for e in book if (e.get("answer") or "").strip())
    return (
        f"JOB: {job_title} at {company}\n\nJOB DESCRIPTION (trimmed):\n{(job_description or '')[:6000]}\n\n"
        f"CANDIDATE RÉSUMÉ:\n{(resume_text or '')[:9000]}\n\n"
        f"CANDIDATE'S SAVED ANSWERS:\n{entries[:3000] or '(none)'}"
    )


def draft_answer(
    *,
    question: str,
    description: str = "",
    job_title: str,
    company: str,
    job_description: str,
    resume_text: str,
    book: list[dict],
    client=None,
) -> Draft:
    from app.ai_client import AIRefused, AIUnavailable, complete

    material = _material(resume_text, book, job_title, company, job_description)
    q = question.strip() + (f"\n(Form note: {description.strip()})" if description and description.strip() else "")
    try:
        text, _ = complete(f"{material}\n\nQUESTION ON THE FORM:\n{q}", system=_DRAFT_SYSTEM, answer_tokens=700, client=client)
        data = _json_block(text)
    except AIUnavailable:
        return Draft(error="AI is not configured")
    except AIRefused as exc:
        return Draft(error=f"the model declined: {exc.category or 'unspecified'}")
    except Exception as exc:  # network, JSON — never break the apply path
        logger.info("answer_drafts: draft failed for %r: %s", question[:60], exc)
        return Draft(error="draft failed")
    if not data.get("enough_information") or not (data.get("answer") or "").strip():
        return Draft(enough_information=False, note="Your résumé and saved answers don't cover this, so nothing was drafted. Write it in your own words.")
    answer = re.sub(r"\s*—\s*", ", ", str(data["answer"]).strip())

    # Second pass: every claim must trace back to the material.
    try:
        vtext, _ = complete(f"{material}\n\nANSWER TO CHECK:\n{answer}", system=_VERIFY_SYSTEM, answer_tokens=400, client=client)
        claims = [str(c).strip() for c in (_json_block(vtext).get("unsupported_claims") or []) if str(c).strip()]
    except Exception as exc:
        logger.info("answer_drafts: verify failed for %r: %s", question[:60], exc)
        claims = []
        note = "Drafted from your résumé and the job description. The fact-check pass failed, so read it carefully."
        return Draft(text=answer, enough_information=True, note=note)
    if claims:
        return Draft(text=answer, enough_information=True, unsupported_claims=claims,
                     note="Drafted from your résumé and the job description, but these claims could not be traced to your material. Edit them out or correct them before using it: " + "; ".join(claims[:4]))
    return Draft(text=answer, enough_information=True, note="Drafted from your résumé and the job description. Every claim traced back to your material. Edit freely, then save.")


_OWN_WORDS_RE = re.compile(
    r"(refrain from|do not|don'?t|no|without|avoid) (the )?(use of |using |use )?(an? )?(ai|chatgpt|llm|generative)"
    r"|in your own words|your own words|written by you|not (be )?ai[- ]generated|ai[- ]generated (answers|responses|content) (are|is|will be) not",
    re.I,
)


def employer_wants_own_words(*texts: str) -> bool:
    """The employer asked for the candidate's own words (Modular: "Please
    refrain from using AI to complete answer this questions"). We honour
    that: no draft, and the note says why."""
    return any(_OWN_WORDS_RE.search(t or "") for t in texts)


def draftable(gap_field: dict) -> bool:
    """A required free-text question we may draft for: text/textarea, not
    legal/protected/compensation, not an identity field, and not one the
    employer asked the candidate to write themselves."""
    from app.workers.tasks._answer_prep import is_never_infer_field

    if gap_field.get("field_type") not in ("text", "textarea"):
        return False
    if employer_wants_own_words(gap_field.get("label", ""), gap_field.get("description", "")):
        return False
    if is_never_infer_field(gap_field.get("field_key", ""), gap_field.get("label", "")):
        return False
    key = (gap_field.get("field_key") or "").lower()
    if key in ("first_name", "last_name", "email", "phone", "linkedin", "linkedin_url", "address", "city", "postcode",
               "country", "state", "location", "preferred_name", "website", "github", "referred_by"):
        return False
    return True
