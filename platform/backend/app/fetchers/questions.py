"""Fetch application form questions from ATS platforms.

Each platform exposes different endpoints for retrieving the actual
application form fields a candidate must fill in. This module provides
a unified interface to fetch and normalize those fields.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Platforms with a real question extractor below. Everything else falls
# back to `_STANDARD_FIELDS`, which is a *guess* at the form, not the
# form. Callers must branch on this rather than assume the returned
# schema reflects the actual posting — see `extraction_mode`.
#
# F346/F348. We list jobs from ~20 platforms (see fetchers/__init__) but
# can only extract real application questions from these. The gap is the
# single biggest limit on safe auto-apply: for a Workday or
# SmartRecruiters posting we return eight generic fields, and before
# F346 nothing in the response said "these were invented".
SUPPORTED_QUESTION_PLATFORMS: frozenset[str] = frozenset(
    {"greenhouse", "lever", "ashby", "recruitee"}
)


# ---------------------------------------------------------------------------
# Standard fallback fields for platforms without public form APIs
# ---------------------------------------------------------------------------
_STANDARD_FIELDS: list[dict[str, Any]] = [
    {"field_key": "first_name", "label": "First Name", "field_type": "text", "required": True, "options": [], "description": ""},
    {"field_key": "last_name", "label": "Last Name", "field_type": "text", "required": True, "options": [], "description": ""},
    {"field_key": "email", "label": "Email", "field_type": "text", "required": True, "options": [], "description": ""},
    {"field_key": "phone", "label": "Phone", "field_type": "text", "required": False, "options": [], "description": ""},
    {"field_key": "resume", "label": "Resume / CV", "field_type": "file", "required": True, "options": [], "description": ""},
    {"field_key": "cover_letter", "label": "Cover Letter", "field_type": "textarea", "required": False, "options": [], "description": ""},
    {"field_key": "linkedin_url", "label": "LinkedIn URL", "field_type": "text", "required": False, "options": [], "description": ""},
    {"field_key": "website", "label": "Website / Portfolio", "field_type": "text", "required": False, "options": [], "description": ""},
]


def fetch_application_questions(
    platform: str,
    job_external_id: str,
    board_slug: str,
) -> list[dict[str, Any]]:
    """Fetch application form questions for a specific job.

    Returns a list of normalised question dicts::

        {
            "field_key": "first_name",
            "label": "First Name",
            "field_type": "text",       # text | textarea | select | multi_select | file | boolean
            "required": True,
            "options": [],              # populated for select / multi_select
            "description": "",
        }

    Falls back to a standard set of fields when the platform does not
    expose form questions via a public API.
    """
    fetchers = {
        "greenhouse": _fetch_greenhouse_questions,
        "lever": _fetch_lever_questions,
        "ashby": _fetch_ashby_questions,
        "recruitee": _fetch_recruitee_questions,
    }

    fetcher_fn = fetchers.get(platform)
    if fetcher_fn is None:
        logger.debug("No question fetcher for platform %s; using standard fields", platform)
        return _fallback_fields("unsupported_platform")

    try:
        questions = fetcher_fn(job_external_id, board_slug)
        if questions:
            return [{**q, "extraction_mode": "extracted"} for q in questions]
        # An empty (but successful) response is still a miss: we asked
        # the ATS and it told us nothing, so the standard fields below
        # are guesswork exactly as they are for an unsupported platform.
        return _fallback_fields("empty_response")
    except Exception:
        logger.warning("Failed to fetch questions for %s/%s/%s; using fallback", platform, board_slug, job_external_id, exc_info=True)

    return _fallback_fields("fetch_failed")


def _fallback_fields(reason: str) -> list[dict[str, Any]]:
    """Standard fields, explicitly stamped as a guess.

    F346. Returning `_STANDARD_FIELDS` bare made a guessed form
    indistinguishable from an extracted one, so the apply path treated
    "we think most forms ask for an email" as "this form asks for an
    email". Every fallback field now carries
    ``extraction_mode="fallback"`` plus the reason we fell back, and the
    apply gate refuses to auto-submit a fallback schema.
    """
    return [
        {**field, "extraction_mode": "fallback", "fallback_reason": reason}
        for field in _STANDARD_FIELDS
    ]


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------
# Greenhouse Job Board API: GET /v1/boards/{board_token}/jobs/{id}
# Response includes a "questions" array with nested "fields".
# Docs: https://developers.greenhouse.io/job-board.html#retrieve-a-job

_GH_JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true"

_GH_FIELD_TYPE_MAP = {
    "input_text": "text",
    "input_file": "file",
    "input_hidden": "text",
    "textarea": "textarea",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multi_select",
}


def _fetch_greenhouse_questions(job_id: str, slug: str) -> list[dict[str, Any]]:
    url = _GH_JOB_URL.format(slug=slug, job_id=job_id)

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()

    data = resp.json()
    raw_questions = data.get("questions", [])
    if not raw_questions:
        return []

    results: list[dict[str, Any]] = []
    for q in raw_questions:
        label = q.get("label", "") or ""
        required = q.get("required", False)
        description = q.get("description", "") or ""

        fields = q.get("fields", [])
        if not fields:
            continue

        # F350 — a Greenhouse *question* can carry several *fields* that
        # are alternatives, not all-of. "Resume/CV" (required) exposes
        # both `resume` (input_file) and `resume_text` (textarea):
        # satisfying either satisfies the question. Flattening them into
        # two independently-required fields made an attached resume
        # insufficient, so the gate blocked and the adapter aborted on a
        # `resume_text` box that isn't even rendered until you choose
        # "enter manually". Tag the siblings so downstream can treat the
        # group as one requirement.
        group = (
            f"altgroup_{_normalise_field_key(label)}" if len(fields) > 1 else ""
        )

        for f in fields:
            f_name = f.get("name", "") or ""
            f_type_raw = f.get("type", "input_text") or "input_text"
            f_type = _GH_FIELD_TYPE_MAP.get(f_type_raw, "text")

            options = []
            for v in f.get("values", []):
                if isinstance(v, dict):
                    options.append({"value": str(v.get("value", "")), "label": v.get("label", str(v.get("value", "")))})
                else:
                    options.append({"value": str(v), "label": str(v)})

            field_key = _normalise_field_key(f_name or label)

            results.append({
                "field_key": field_key,
                "label": label,
                "field_type": f_type,
                "required": required,
                "options": options,
                "description": description,
                "alternative_group": group,
            })

    return results


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------
# Lever Postings API: GET /v0/postings/{company}/{id}/apply
# Returns an HTML page but also we can get form structure from /v0/postings/{company}/{id}
# The individual posting JSON includes "lists" and "additional" sections.
# Standard Lever forms always ask: name, email, phone, resume, LinkedIn, website, cover letter.

_LEVER_POSTING_URL = "https://api.lever.co/v0/postings/{slug}/{posting_id}"

_LEVER_STANDARD_FIELDS: list[dict[str, Any]] = [
    {"field_key": "name", "label": "Full Name", "field_type": "text", "required": True, "options": [], "description": ""},
    {"field_key": "email", "label": "Email", "field_type": "text", "required": True, "options": [], "description": ""},
    {"field_key": "phone", "label": "Phone", "field_type": "text", "required": False, "options": [], "description": ""},
    {"field_key": "resume", "label": "Resume / CV", "field_type": "file", "required": True, "options": [], "description": ""},
    {"field_key": "linkedin_url", "label": "LinkedIn URL", "field_type": "text", "required": False, "options": [], "description": ""},
    {"field_key": "website", "label": "Website / Portfolio", "field_type": "text", "required": False, "options": [], "description": ""},
    {"field_key": "cover_letter", "label": "Cover Letter", "field_type": "textarea", "required": False, "options": [], "description": ""},
]


def _fetch_lever_questions(posting_id: str, slug: str) -> list[dict[str, Any]]:
    url = _LEVER_POSTING_URL.format(slug=slug, posting_id=posting_id)

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()

    data = resp.json()

    # Lever individual posting responses include custom question lists
    results = list(_LEVER_STANDARD_FIELDS)

    # "lists" contains custom multi-select / single-select questions
    for lst in data.get("lists", []):
        label = lst.get("text", "") or ""
        content = lst.get("content", "")
        if not label:
            continue
        results.append({
            "field_key": _normalise_field_key(label),
            "label": label,
            "field_type": "textarea",
            "required": False,
            "options": [],
            "description": content or "",
        })

    # "additional" and "additionalPlain" contain custom text question content
    additional = data.get("additional", "") or ""
    additional_plain = data.get("additionalPlain", "") or ""
    if additional_plain:
        # Try to extract questions from the additional plain text
        # Each line that ends with '?' is likely a question
        for line in additional_plain.split("\n"):
            line = line.strip()
            if line.endswith("?") and len(line) > 10:
                results.append({
                    "field_key": _normalise_field_key(line),
                    "label": line,
                    "field_type": "textarea",
                    "required": False,
                    "options": [],
                    "description": "",
                })

    return results


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------
# Ashby Application Form API: POST /posting-api/job-board/{slug}/application-form
# Body: { jobPostingId: "<id>" }
# Returns form field definitions.

_ASHBY_FORM_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}/application-form"

_ASHBY_FIELD_TYPE_MAP = {
    "String": "text",
    "Email": "text",
    "Phone": "text",
    "LongText": "textarea",
    "File": "file",
    "Boolean": "boolean",
    "ValueSelect": "select",
    "MultiValueSelect": "multi_select",
}


def _fetch_ashby_questions(job_id: str, slug: str) -> list[dict[str, Any]]:
    url = _ASHBY_FORM_URL.format(slug=slug)

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.post(url, json={"jobPostingId": job_id})
        resp.raise_for_status()

    data = resp.json()

    # Ashby returns { formDefinition: { sections: [ { fields: [...] } ] } }
    form_def = data.get("formDefinition") or data.get("form") or {}
    sections = form_def.get("sections", [])

    results: list[dict[str, Any]] = []
    for section in sections:
        for field in section.get("fields", []):
            f_path = field.get("path", "") or ""
            f_title = field.get("title", "") or field.get("label", "") or ""
            f_type_raw = field.get("type", "String") or "String"
            f_type = _ASHBY_FIELD_TYPE_MAP.get(f_type_raw, "text")
            required = field.get("isRequired", False)
            description = field.get("descriptionPlain", "") or field.get("description", "") or ""

            options = []
            for opt in field.get("selectableValues", []):
                if isinstance(opt, dict):
                    options.append({"value": str(opt.get("value", "")), "label": opt.get("label", str(opt.get("value", "")))})
                else:
                    options.append({"value": str(opt), "label": str(opt)})

            field_key = _normalise_field_key(f_path or f_title)

            results.append({
                "field_key": field_key,
                "label": f_title,
                "field_type": f_type,
                "required": required,
                "options": options,
                "description": description,
            })

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_field_key(text: str) -> str:
    """Turn a label or field name into a normalised key for matching."""
    key = text.lower().strip()
    key = re.sub(r"[^\w\s]", "", key)
    key = re.sub(r"\s+", "_", key)
    return key[:255]


# ---------------------------------------------------------------------------
# Recruitee
# ---------------------------------------------------------------------------
# Recruitee public offers API: GET https://{slug}.recruitee.com/api/offers/{id}
# The offer detail carries an ``open_questions`` array — the custom
# questions a candidate must answer. Verified against a live board
# (multiplier.recruitee.com, offer 2697445) rather than from docs; the
# shapes below are what that endpoint actually returns.
#
#   {"id": 4281786, "kind": "multi_choice", "required": true,
#    "body": "Which areas you feel are your strongest:",
#    "open_question_options": [{"id": 6588542, "body": "Market research"}, ...]}
#
# ``kind`` values observed: text, string, multi_choice, boolean, video.

_RECRUITEE_OFFER_URL = "https://{slug}.recruitee.com/api/offers/{offer_id}"

_RECRUITEE_KIND_MAP = {
    # `text` is Recruitee's long-form answer, `string` its single-line one.
    "text": "textarea",
    "string": "text",
    "multi_choice": "select",
    "boolean": "boolean",
    # A video answer cannot be produced unattended. Typing it as `file`
    # means the apply gate leaves it unanswered, and if it's required
    # `blocking_gaps` routes the application to needs_user — which is
    # the correct outcome, not a bug.
    "video": "file",
}

# Recruitee renders the same identity block on every offer regardless of
# the custom questions, and the offers API does not describe it. These
# are platform behaviour, not a guess about a particular posting, so
# they ship as extracted fields.
_RECRUITEE_FIXED_FIELDS: list[dict[str, Any]] = [
    {"field_key": "name", "label": "Full Name", "field_type": "text", "required": True, "options": [], "description": ""},
    {"field_key": "email", "label": "Email", "field_type": "text", "required": True, "options": [], "description": ""},
    {"field_key": "phone", "label": "Phone", "field_type": "text", "required": False, "options": [], "description": ""},
    {"field_key": "resume", "label": "Resume / CV", "field_type": "file", "required": True, "options": [], "description": ""},
    {"field_key": "cover_letter", "label": "Cover Letter", "field_type": "textarea", "required": False, "options": [], "description": ""},
]


def _recruitee_offer_id(job_external_id: str) -> str:
    """Strip the ``recruitee-`` prefix our fetcher adds to external_id.

    ``app/fetchers/recruitee.py`` stores ``f"recruitee-{job_id}"`` so the
    global UNIQUE on ``jobs.external_id`` can't collide with another
    ATS's numeric ids. The API wants the bare id back.
    """
    raw = (job_external_id or "").strip()
    return raw[len("recruitee-"):] if raw.startswith("recruitee-") else raw


def _fetch_recruitee_questions(job_external_id: str, slug: str) -> list[dict[str, Any]]:
    offer_id = _recruitee_offer_id(job_external_id)
    if not offer_id or not slug:
        return []

    url = _RECRUITEE_OFFER_URL.format(slug=slug, offer_id=offer_id)
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()

    offer = payload.get("offer") or {}
    fields: list[dict[str, Any]] = list(_RECRUITEE_FIXED_FIELDS)

    for q in offer.get("open_questions") or []:
        if not isinstance(q, dict):
            continue
        qid = q.get("id")
        body = (q.get("body") or "").strip()
        if qid is None or not body:
            continue
        options = [
            (opt.get("body") or "").strip()
            for opt in (q.get("open_question_options") or [])
            if isinstance(opt, dict) and (opt.get("body") or "").strip()
        ]
        fields.append({
            # Key by the ATS's own question id so the schema we show and
            # the DOM we fill stay addressable by the same value.
            "field_key": f"open_question_{qid}",
            "label": body,
            "field_type": _RECRUITEE_KIND_MAP.get(q.get("kind") or "", "text"),
            "required": bool(q.get("required")),
            "options": options,
            "description": "",
        })

    return fields
