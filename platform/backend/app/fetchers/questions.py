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
# F346. We list jobs from ~20 platforms (see fetchers/__init__) but can
# only extract real application questions from these three. The gap is
# the single biggest limit on safe auto-apply: for a Workday or
# SmartRecruiters posting we currently return eight generic fields and
# nothing in the response says "these were invented".
SUPPORTED_QUESTION_PLATFORMS: frozenset[str] = frozenset(
    {"greenhouse", "lever", "ashby"}
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
