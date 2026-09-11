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

# Lever's apply page gates plain scripted clients on User-Agent.
_BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

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
    {"greenhouse", "recruitee", "lever", "workable", "ashby", "bamboohr", "breezy", "personio", "rippling", "jazzhr", "teamtailor", "pinpoint"}
)

# F368 — platforms whose application form is behind a wall only a person
# can pass. Each entry was established on a live board, not assumed:
#
#   lever           hCaptcha checkbox on every apply page (F359).
#   bamboohr        reCAPTCHA v2, ``size=normal`` anchor + bframe, plus a
#                   "please leave this field blank" honeypot — verified
#                   on icmarkets.bamboohr.com/careers/128.
#   smartrecruiters DataDome in front of the oneclick-ui apply form. It
#                   served the CAPTCHA interstitial to headless, new-
#                   headless AND headed Chromium, and eventually to a
#                   real Chrome on the same IP — reputation-based, so a
#                   server will always be challenged. Getting past it is
#                   detection evasion, which we do not do.
#
# Ashby is deliberately NOT here: its reCAPTCHA is invisible v3, which
# mints its token on submit with nobody clicking anything (same class
# as Greenhouse's enterprise build). ``base._interactive_recaptcha``
# makes that distinction at the DOM level; this map makes it at the
# platform level so the review queue can say *why* a form needs you
# instead of "no submitter".
KNOWN_HUMAN_WALLS: dict[str, dict[str, str]] = {
    "lever": {
        "vendor": "hCaptcha",
        "reason": "Lever puts an hCaptcha checkbox on every application, so a person has to submit it.",
    },
    "bamboohr": {
        "vendor": "reCAPTCHA",
        "reason": "BambooHR puts an \"I'm not a robot\" reCAPTCHA on its application form, so a person has to submit it.",
    },
    "jazzhr": {
        "vendor": "reCAPTCHA",
        "reason": "JazzHR puts an \"I'm not a robot\" reCAPTCHA on its application form, so a person has to submit it.",
    },
    "smartrecruiters": {
        "vendor": "DataDome",
        "reason": "SmartRecruiters protects its application form with DataDome bot detection, which challenges automated browsers, so it has to be applied in your own browser.",
    },
}


def human_wall_for(platform: str) -> dict[str, str] | None:
    """The wall on a platform's form, if we know of one."""
    return KNOWN_HUMAN_WALLS.get((platform or "").strip().lower())


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


def _normalise_field_key(text: str) -> str:
    """Normalise a raw field name / label into a stable key.

    Restored in F366: the definition lived inside the old Ashby block
    that F366 replaced, and Greenhouse, Lever and Workable all call it.
    The fallback path masked the NameError as "fetch failed".
    """
    key = (text or "").lower().strip()
    key = re.sub(r"[^\w\s]", "", key)
    key = re.sub(r"\s+", "_", key)
    return key[:255]


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
        "workable": _fetch_workable_questions,
        "ashby": _fetch_ashby_questions,
        "bamboohr": _fetch_bamboohr_questions,
        "recruitee": _fetch_recruitee_questions,
        "breezy": _fetch_breezy_questions,
        "personio": _fetch_personio_questions,
        "rippling": _fetch_rippling_questions,
        "jazzhr": _fetch_jazzhr_questions,
        "teamtailor": _fetch_teamtailor_questions,
        "pinpoint": _fetch_pinpoint_questions,
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
    """Read Lever's real application form.

    F358. The previous implementation (removed in F357) never read a
    form at all — it returned a template and turned job-description
    headings into questions. Lever's postings API genuinely doesn't
    expose the form, but the apply *page* is server-rendered: a plain
    GET of ``/{slug}/{id}/apply`` returns the complete markup, so this
    needs no browser.

    Every field lives in an ``<li class="application-question">`` (or
    ``application-field``) carrying a ``.application-label`` and one or
    more inputs. Three families matter:

    * fixed identity — ``name``, ``email``, ``phone``, ``location``,
      ``org``, ``urls[LinkedIn]`` and friends, ``resume`` (file)
    * custom questions — ``cards[<uuid>][...]``, rendered as radios,
      checkboxes or textareas
    * EEO surveys — ``surveysResponses[<uuid>]``

    The field_key IS the input's ``name``, so the schema we show and the
    DOM a submitter would fill stay keyed identically.
    """
    from bs4 import BeautifulSoup

    url = f"https://jobs.lever.co/{slug}/{posting_id}/apply"
    with httpx.Client(timeout=20, follow_redirects=True, headers=_BROWSER_UA) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for block in soup.select("li.application-question, li.application-field"):
        label_el = block.select_one(".application-label")
        label = " ".join((label_el.get_text(" ") if label_el else "").split())
        # Lever marks required with a "✱" glyph inside the label.
        required = "✱" in label or block.select_one("[required]") is not None
        label = label.replace("✱", "").strip()

        inputs = [
            el for el in block.select("input, textarea, select")
            if (el.get("type") or "").lower() != "hidden"
        ]
        if not inputs:
            continue

        by_name: dict[str, list] = {}
        for el in inputs:
            name = el.get("name")
            if name:
                by_name.setdefault(name, []).append(el)

        for name, els in by_name.items():
            if name in seen:
                continue
            seen.add(name)
            first = els[0]
            tag = first.name.lower()
            itype = (first.get("type") or "").lower()

            if tag == "textarea":
                ftype = "textarea"
            elif tag == "select":
                ftype = "select"
            elif itype == "file":
                ftype = "file"
            elif itype == "radio":
                ftype = "select"
            elif itype == "checkbox":
                # One checkbox is a yes/no; several sharing a name is a
                # pick-many list (Lever's pronouns block, for example).
                ftype = "multi_select" if len(els) > 1 else "boolean"
            else:
                ftype = "text"

            options: list[str] = []
            if itype in ("radio", "checkbox") and len(els) > 1:
                for el in els:
                    # The visible choice text is the input's own label.
                    holder = el.find_parent("label") or el.parent
                    text = " ".join((holder.get_text(" ") if holder else "").split())
                    if text:
                        options.append(text)
            elif tag == "select":
                options = [
                    " ".join(o.get_text(" ").split())
                    for o in first.select("option")
                    if o.get_text(strip=True)
                ]

            results.append({
                "field_key": name,
                "label": label or name,
                "field_type": ftype,
                "required": bool(required),
                "options": options,
                "description": "",
            })

    return results


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------
# F366. The posting-api application-form endpoint 401s for every public
# board (it needs the employer's key), which is why F357 unwired Ashby.
# That was the wrong probe: the application PAGE
# (jobs.ashbyhq.com/{org}/{id}/application) renders fully in headless
# Chromium — verified on ramp, supabase, linear and vanta (13–33 fields,
# no shadow DOM, no bot wall). So extraction goes through the page.
#
# Every board also carries a reCAPTCHA v2 checkbox, so unattended
# SUBMISSION is gated (same class as Lever): Ashby is extraction-only and
# routes to the review queue, where the never-infer gate still flags the
# sponsorship/salary questions and the human submits from their browser.
#
# Field shapes (live): system fields carry stable ids (_systemfield_name,
# _systemfield_email, _systemfield_resume); everything else is a UUID that
# is both id and name, labelled via label[for]. A lone checkbox is a yes/no
# question whose text lives on the enclosing _fieldEntry wrapper. Radios
# share a name (communicationConsent) and are labelled by their option.

_ASHBY_APPLY_URL = "https://jobs.ashbyhq.com/{slug}/{job_id}/application"
_ASHBY_FORM_READY = "#_systemfield_name, input[name='_systemfield_name']"

_ASHBY_STRUCT_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const rows = [];
  for (const e of document.querySelectorAll('input,select,textarea')) {
    if (e.type === 'hidden') continue;
    const name = e.name || '';
    const id = e.id || '';
    if (name === 'g-recaptcha-response') continue;
    let label = '';
    if (id) label = norm(document.querySelector(`label[for="${CSS.escape(id)}"]`)?.innerText || '');
    if (!label) label = norm(e.getAttribute('aria-label') || '');
    const wrap = e.closest('[class*="_fieldEntry"]');
    const wrapLabel = wrap ? norm(wrap.querySelector('label, [class*="label"], [class*="Label"]')?.innerText || '') : '';
    const own = e.closest('label') ? norm(e.closest('label').innerText) : '';
    rows.push({ tag: e.tagName.toLowerCase(), type: e.type, name, id, label, wrapLabel, option: own,
                required: !!(e.required || e.getAttribute('aria-required') === 'true') });
  }
  return rows;
})()
"""


def ashby_form_rows(job_id: str, slug: str) -> list[dict[str, Any]]:
    """Raw per-input rows from the rendered application page (browser)."""
    from app.services.playwright_browser import BrowserSession

    async def _go():
        async with BrowserSession() as session:
            await session.navigate(
                _ASHBY_APPLY_URL.format(slug=slug, job_id=job_id),
                wait_until="domcontentloaded",
                wait_for_selector=_ASHBY_FORM_READY,
            )
            return await session.eval_js(_ASHBY_STRUCT_JS) or []

    return _run_in_fresh_loop(_go())


def normalise_ashby_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn raw input rows into our question schema (pure, testable)."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    radio_groups: dict[str, dict[str, Any]] = {}

    for r in rows:
        typ = (r.get("type") or "").lower()
        name, rid = r.get("name") or "", r.get("id") or ""
        label = r.get("label") or r.get("wrapLabel") or ""

        if typ == "file":
            if rid == "_systemfield_resume":
                if "resume" not in seen:
                    seen.add("resume")
                    results.append({"field_key": "resume", "label": "Resume / CV", "field_type": "file",
                                    "required": bool(r.get("required")), "options": [], "description": ""})
            elif label.lower().startswith("cover"):
                if "cover_letter_file" not in seen:
                    seen.add("cover_letter_file")
                    results.append({"field_key": "cover_letter_file", "label": label or "Cover Letter",
                                    "field_type": "file", "required": bool(r.get("required")), "options": [], "description": ""})
            continue

        if typ == "radio":
            g = radio_groups.get(name)
            if g is None:
                g = radio_groups[name] = {"field_key": name, "label": r.get("wrapLabel") or name, "field_type": "select",
                                          "required": bool(r.get("required")), "options": [], "description": ""}
                results.append(g)
            opt = r.get("option") or r.get("label") or ""
            if opt and opt not in g["options"]:
                g["options"].append(opt)
            continue

        if typ == "checkbox":
            key = name or rid
            if not key or key in seen:
                continue
            seen.add(key)
            results.append({"field_key": key, "label": r.get("wrapLabel") or label or key, "field_type": "boolean",
                            "required": bool(r.get("required")), "options": [], "description": ""})
            continue

        key = rid or name
        if not key:
            wl = r.get("wrapLabel") or ""
            if not wl:
                continue
            key = f"ashby_{_normalise_field_key(wl)}"
        if key in seen:
            continue
        seen.add(key)
        canonical = {"_systemfield_name": ("name", "Full Name"), "_systemfield_email": ("email", "Email")}.get(key)
        if canonical:
            key, label = canonical
        elif label.lower() == "phone":
            key = "phone"
        ftype = "textarea" if r.get("tag") == "textarea" else "text"
        results.append({"field_key": key, "label": label or key, "field_type": ftype,
                        "required": bool(r.get("required")), "options": [], "description": ""})

    for g in radio_groups.values():
        if g["field_key"] == "communicationConsent":
            g["label"] = "Communication consent (SMS)"
    return results


def _fetch_ashby_questions(job_id: str, slug: str) -> list[dict[str, Any]]:
    if not job_id or not slug:
        return []
    return normalise_ashby_rows(ashby_form_rows(job_id, slug))


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

# F354 — enumerated across every offer on two live boards (channable,
# tellenthr) rather than inferred from one posting. Frequencies observed:
# string 47, boolean 13, text 9, single_choice 8, video 7, file 6,
# multi_choice 1. Unmapped kinds fall through to "text", which silently
# discards a choice question's options — so the map has to be complete,
# not representative.
_RECRUITEE_KIND_MAP = {
    # `text` is Recruitee's long-form answer, `string` its single-line one.
    "text": "textarea",
    "string": "text",
    # Recruitee ships BOTH kinds, which settles the ambiguity: with
    # `single_choice` present as its own kind, `multi_choice` really does
    # mean pick-many. Mapping it to `select` (as the first draft did)
    # would have submitted one answer to a question expecting several.
    "single_choice": "select",
    "multi_choice": "multi_select",
    "boolean": "boolean",
    # Neither of these can be produced unattended. Typing them as `file`
    # means the apply gate leaves them unanswered, and if required,
    # `blocking_gaps` routes the application to needs_user — the correct
    # outcome, not a bug. `file` is a real kind here: several postings
    # ask for a motivation letter as an attachment.
    "video": "file",
    "file": "file",
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


# ---------------------------------------------------------------------------
# Workable
# ---------------------------------------------------------------------------
# F365. Workable exposes the job (GET /api/v2/accounts/{slug}/jobs/{code})
# but not its application form — every /form variant 404s — and the apply
# page is client-rendered: plain HTTP returns a 7.5 KB shell with zero
# inputs, while a browser renders ~190 KB with the real form. So this
# extractor loads the page in Chromium. Verified on two live boards
# (deeplight, payabl): no bot wall, no shadow DOM, no native <select>.
#
# Every field sits in a wrapper carrying data-ui. Fixed fields use their
# own name (firstname, lastname, email, headline, phone, address,
# summary, cover_letter; the resume is `[data-ui=resume] input[type=file]`).
# Custom questions are `QA_<id>`: free text as <textarea id=name=QA_…>
# with a label[for] carrying the question; choices as radios sharing the
# QA name, each option inside `[data-ui=option]`, the question on the
# enclosing `[data-ui=QA_…]`.
#
# Called synchronously from inside an *async* endpoint (get_or_fetch_
# questions is async; this dispatcher is sync), so asyncio.run() would
# raise "already running". The coroutine runs on a throwaway thread with
# its own loop; the browser pool is loop-bound (F353) and shut down
# inside that loop so nothing leaks.

_WORKABLE_APPLY_URL = "https://apply.workable.com/{slug}/j/{shortcode}/apply/"
_WORKABLE_FORM_READY = '[data-ui="firstname"] input, input[name="firstname"]'

_WORKABLE_STRUCT_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').replace(/^\*\s*/, '').trim();
  const rows = [];
  for (const e of document.querySelectorAll('input,select,textarea')) {
    if (e.type === 'hidden') continue;
    const name = e.name || '';
    const dataUi = e.getAttribute('data-ui') || '';
    let question = '';
    let option = '';
    if (e.type === 'radio' || e.type === 'checkbox') {
      const grp = e.closest('fieldset[data-ui^="QA_"], fieldset');
      const optionTexts = grp ? [...grp.querySelectorAll('label')].map(l => norm(l.innerText)) : [];
      // The fieldset holds only the options ("YES NO"); the question text
      // lives on an ancestor — verified live it is two levels up. Walk up
      // until stripping the option texts leaves something, but stop at
      // anything long enough to be a whole section.
      let host = grp, q = '';
      for (let i = 0; host && i < 4 && !q; i++) {
        let t = norm(host.innerText);
        for (const o of optionTexts) { if (o) t = t.replace(o, ''); }
        t = norm(t);
        if (t && t.length < 220) q = t;
        host = host.parentElement;
      }
      question = q;
      option = norm((e.closest('label') || e.nextElementSibling)?.innerText || e.value || '');
    } else {
      const lab = e.closest('label');
      if (lab) question = norm(lab.innerText.replace(e.value || '', ''));
      if (!question && e.id) question = norm(document.querySelector(`label[for="${CSS.escape(e.id)}"]`)?.innerText || '');
    }
    rows.push({ tag: e.tagName.toLowerCase(), type: e.type, name, id: e.id || '', dataUi,
                required: !!(e.required || e.getAttribute('aria-required') === 'true'),
                question, option, value: e.value || '' });
  }
  return rows;
})()
"""

# Fixed Workable fields -> our canonical keys + labels. Anything else with a
# non-QA name is passed through under its own name.
_WORKABLE_FIXED: dict[str, tuple[str, str, str]] = {
    "firstname":    ("first_name",   "First Name",   "text"),
    "lastname":     ("last_name",    "Last Name",    "text"),
    "email":        ("email",        "Email",        "text"),
    "phone":        ("phone",        "Phone",        "text"),
    "headline":     ("headline",     "Headline",     "text"),
    "address":      ("address",      "Address",      "text"),
    "city":         ("city",         "City",         "text"),
    "postcode":     ("postcode",     "Postcode",     "text"),
    "country":      ("country",      "Country",      "text"),
    "summary":      ("summary",      "Summary",      "textarea"),
    "cover_letter": ("cover_letter", "Cover Letter", "textarea"),
}


def _run_in_fresh_loop(coro):
    """Run a coroutine to completion on a throwaway thread + loop.

    Needed because this sync fetcher is invoked from inside a running
    event loop (the async questions endpoint). The pool is torn down
    inside the same loop so Chromium doesn't leak (F353).
    """
    import asyncio
    import concurrent.futures

    from app.services.playwright_browser import shutdown_pool

    async def _wrapped():
        try:
            return await coro
        finally:
            try:
                await shutdown_pool()
            except Exception:  # never mask the extraction result
                logger.warning("workable: browser shutdown failed", exc_info=True)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_wrapped())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, _wrapped()).result()


def workable_form_rows(shortcode: str, slug: str) -> list[dict[str, Any]]:
    """Raw per-input rows from the rendered apply page (browser)."""
    from app.services.playwright_browser import BrowserSession

    async def _go():
        async with BrowserSession() as session:
            await session.navigate(
                _WORKABLE_APPLY_URL.format(slug=slug, shortcode=shortcode),
                wait_until="domcontentloaded",
                wait_for_selector=_WORKABLE_FORM_READY,
            )
            return await session.eval_js(_WORKABLE_STRUCT_JS) or []

    return _run_in_fresh_loop(_go())


def normalise_workable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn raw input rows into our question schema (pure, testable)."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    groups: dict[str, dict[str, Any]] = {}

    for r in rows:
        name = r.get("name") or ""
        typ = (r.get("type") or "").lower()
        data_ui = r.get("dataUi") or ""

        if typ == "file":
            if data_ui == "resume" and "resume" not in seen:
                seen.add("resume")
                results.append({"field_key": "resume", "label": "Resume / CV", "field_type": "file",
                                "required": bool(r.get("required")), "options": [], "description": ""})
            continue

        if typ in ("radio", "checkbox"):
            # Radios share the QA name; checkbox options each carry their own
            # numeric name, so group them by the enclosing question instead.
            gkey = name if name.startswith("QA_") else (r.get("question") or data_ui or name)
            g = groups.get(gkey)
            if g is None:
                g = groups[gkey] = {
                    "field_key": name if name.startswith("QA_") else f"group_{_normalise_field_key(gkey)}",
                    "label": r.get("question") or gkey, "field_type": "select" if typ == "radio" else "multi_select",
                    "required": bool(r.get("required")), "options": [], "description": "",
                    "_names": [],
                }
                results.append(g)
            opt = r.get("option") or r.get("value") or ""
            if opt and opt not in g["options"]:
                g["options"].append(opt)
            g["_names"].append(name)
            continue

        if not name or name in seen:
            continue
        seen.add(name)
        if name in _WORKABLE_FIXED:
            key, label, ftype = _WORKABLE_FIXED[name]
        elif name.startswith("QA_"):
            key, label, ftype = name, (r.get("question") or name), ("textarea" if r["tag"] == "textarea" else "text")
        else:
            key, label, ftype = name, (r.get("question") or name), ("textarea" if r["tag"] == "textarea" else "text")
        results.append({"field_key": key, "label": label, "field_type": ftype,
                        "required": bool(r.get("required")), "options": [], "description": ""})

    for g in groups.values():
        g.pop("_names", None)
    return results


def _fetch_workable_questions(shortcode: str, slug: str) -> list[dict[str, Any]]:
    if not shortcode or not slug:
        return []
    return normalise_workable_rows(workable_form_rows(shortcode, slug))


# ---------------------------------------------------------------------------
# BambooHR
# ---------------------------------------------------------------------------
# F367. GET https://{slug}.bamboohr.com/careers/{id}/detail returns JSON
# with a complete ``formFields`` map — the cleanest form source since
# Greenhouse, and plain HTTP. Verified on a live board (icmarkets, job
# 128). Fixed fields carry {isRequired, label, options?}; custom screening
# questions sit in ``formFields.customQuestions`` as
#   {id, isRequired, question, type, options:[{id, option}], hasOther}
# with ``type`` in {short, long, yes_no, checkbox, multi}. EEO fields
# (genderId, ethnicityId, veteranStatusId, disabilityId) are option lists
# that are empty when the board doesn't ask.

_BAMBOO_DETAIL_URL = "https://{slug}.bamboohr.com/careers/{job_id}/detail"

_BAMBOO_FIXED: dict[str, tuple[str, str]] = {
    # bamboo key -> (our key, field_type)
    "firstName": ("first_name", "text"),
    "lastName": ("last_name", "text"),
    "email": ("email", "text"),
    "phone": ("phone", "text"),
    "streetAddress": ("address", "text"),
    "city": ("city", "text"),
    "state": ("state", "text"),
    "zip": ("postcode", "text"),
    "countryId": ("country", "select"),
    "linkedinUrl": ("linkedin_url", "text"),
    "websiteUrl": ("website", "text"),
    "dateAvailable": ("start_date", "text"),
    "resumeFileId": ("resume", "file"),
    "coverLetterFileId": ("cover_letter_file", "file"),
    "desiredPay": ("salary", "text"),
    "referredBy": ("referred_by", "text"),
}
_BAMBOO_EEO: dict[str, str] = {
    "genderId": "gender", "ethnicityId": "race", "veteranStatusId": "veteran_status", "disabilityId": "disability_status",
}
_BAMBOO_QTYPE = {"short": "text", "long": "textarea", "yes_no": "boolean", "checkbox": "boolean", "multi": "select"}


def _bamboo_job_id(job_external_id: str) -> str:
    """Recover the numeric id from our fetcher's ``bamboo-{slug}-{id}``.

    ``app/fetchers/bamboohr.py`` stores ``f"bamboo-{slug}-{job_id}"`` so
    the global UNIQUE on jobs.external_id can't collide across tenants.
    The detail endpoint wants the bare id. Slugs can themselves contain
    hyphens, so take the trailing segment rather than splitting once.
    """
    raw = (job_external_id or "").strip()
    if raw.startswith(("bamboo-", "bamboohr-")):
        return raw.rsplit("-", 1)[-1]
    return raw


def normalise_bamboohr_form(form_fields: dict) -> list[dict[str, Any]]:
    """Turn the detail endpoint's formFields into our schema (pure, testable)."""
    results: list[dict[str, Any]] = []
    for key, spec in (form_fields or {}).items():
        if key == "customQuestions":
            for q in spec or []:
                if not isinstance(q, dict) or not q.get("question"):
                    continue
                qtype = _BAMBOO_QTYPE.get((q.get("type") or "").lower(), "text")
                options = [str(o.get("option") or o.get("text") or "").strip()
                           for o in (q.get("options") or []) if isinstance(o, dict)]
                options = [o for o in options if o]
                if qtype == "select" and str(q.get("hasOther", "")).lower() == "yes":
                    options.append("Other")
                results.append({"field_key": f"bamboo_q_{q.get('id')}", "label": str(q["question"]).strip(),
                                "field_type": qtype, "required": bool(q.get("isRequired")),
                                "options": options, "description": ""})
            continue
        if key in _BAMBOO_EEO:
            opts = [str(o.get("text") or o.get("option") or "").strip() for o in (spec or []) if isinstance(o, dict)]
            if opts:  # empty list = the board doesn't ask
                results.append({"field_key": _BAMBOO_EEO[key], "label": _BAMBOO_EEO[key].replace("_", " ").title(),
                                "field_type": "select", "required": False, "options": opts, "description": ""})
            continue
        if not isinstance(spec, dict):
            continue
        our_key, ftype = _BAMBOO_FIXED.get(key, (key, "text"))
        options = [str(o.get("text") or o.get("option") or "").strip() for o in (spec.get("options") or []) if isinstance(o, dict)]
        if ftype == "select" and not options:
            ftype = "text"  # e.g. `state` ships options: [] on non-US boards
        results.append({"field_key": our_key, "label": str(spec.get("label") or our_key).strip(),
                        "field_type": ftype, "required": bool(spec.get("isRequired")),
                        "options": [o for o in options if o], "description": ""})
    return results


def _fetch_bamboohr_questions(job_external_id: str, slug: str) -> list[dict[str, Any]]:
    job_id = _bamboo_job_id(job_external_id)
    if not job_id or not slug:
        return []
    with httpx.Client(timeout=20, follow_redirects=True, headers=_BROWSER_UA) as client:
        resp = client.get(_BAMBOO_DETAIL_URL.format(slug=slug, job_id=job_id))
        resp.raise_for_status()
        data = resp.json()
    return normalise_bamboohr_form((data.get("result") or data).get("formFields") or {})


# ---------------------------------------------------------------------------
# Breezy HR — F377, read from the rendered apply page
# ---------------------------------------------------------------------------
# Verified on vetsez.breezy.hr/p/{id}/apply. Angular form, no captcha, no
# shadow DOM. Fixed fields have stable names (cName, cEmail, cPhoneNumber,
# cSalary + an unnamed salary-period <select>, cResume, smsConsent,
# ccpaAgreement). Custom questions are ``section_{id}_question_{n}`` whose
# text is the <h3> inside the enclosing ``li.question``; EEO questions are
# radio groups (race_ethnicity, gender, eeoc.veteran_status,
# eeoc.disability_status) whose options are ``li.option label[for]``. A
# honeypot text input (``hp_*``, inside ``.apply-field-extra``) must never
# be filled — it is dropped here so no adapter can see it.

_BREEZY_APPLY_URL = "{url}/apply"
_BREEZY_FORM_READY = "input[name='cName']"

_BREEZY_STRUCT_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const rows = [];
  let lastH3 = '';
  for (const e of document.querySelectorAll('form h3, form input, form textarea, form select')) {
    if (e.tagName === 'H3') { lastH3 = norm((e.querySelector('span') || e).innerText); continue; }
    if (e.type === 'hidden') continue;
    const q = e.closest('li.question');
    const qh = q ? q.querySelector('h3') : null;
    const question = qh ? norm((qh.querySelector('span') || qh).innerText) : lastH3;
    const required = !!(e.required || (q && q.querySelector('h3 .required')) || (qh === null && e.closest('.section') && /\*$/.test(lastH3)));
    const own = e.labels && e.labels[0] ? norm(e.labels[0].innerText) : '';
    const opts = e.tagName === 'SELECT' ? [...e.options].map(o => norm(o.textContent)).filter(Boolean) : [];
    rows.push({ tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', id: e.id || '',
                question: question.replace(/\*$/, '').trim(), option: own, options: opts, required,
                honeypot: !!(e.closest('.apply-field-extra')) || /^hp_/.test(e.name || ''),
                value: e.value || '' });
  }
  return rows;
})()
"""

_BREEZY_FIXED: dict[str, tuple[str, str, str]] = {
    "cName": ("name", "Full Name", "text"),
    "cEmail": ("email", "Email Address", "text"),
    "cPhoneNumber": ("phone", "Phone Number", "text"),
    "cSalary": ("salary", "Desired Salary", "text"),
    "cResume": ("resume", "Resume / CV", "file"),
    "smsConsent": ("sms_consent", "Consent to SMS updates", "boolean"),
    "ccpaAgreement": ("privacy_consent", "I've read the Privacy Notice and consent to the processing of my data", "boolean"),
    # Seen on other live boards (nationsbenefits, clarity-rcm).
    "cLocation": ("location", "Location", "text"),
    "cSummary": ("summary", "Summary", "textarea"),
    "cCoverLetter": ("cover_letter", "Cover Letter", "textarea"),
}


def breezy_form_rows(job_url: str) -> list[dict[str, Any]]:
    from app.services.playwright_browser import BrowserSession

    async def _go():
        async with BrowserSession() as session:
            await session.navigate(
                _BREEZY_APPLY_URL.format(url=job_url.rstrip("/")),
                wait_until="domcontentloaded",
                wait_for_selector=_BREEZY_FORM_READY,
            )
            return await session.eval_js(_BREEZY_STRUCT_JS) or []

    return _run_in_fresh_loop(_go())


def normalise_breezy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows → our question schema (pure, testable)."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    groups: dict[str, dict[str, Any]] = {}
    saw_salary = False

    for r in rows:
        if r.get("honeypot"):
            continue
        name, tag, typ = r.get("name") or "", r.get("tag"), (r.get("type") or "").lower()
        question = r.get("question") or ""

        if typ == "radio":
            g = groups.get(name)
            if g is None:
                g = groups[name] = {"field_key": name, "label": question or name, "field_type": "select",
                                    "required": bool(r.get("required")), "options": [], "description": ""}
                results.append(g)
            opt = r.get("option") or ""
            if opt and opt not in g["options"]:
                g["options"].append(opt)
            continue

        if name in _BREEZY_FIXED:
            key, label, ftype = _BREEZY_FIXED[name]
            if key in seen:
                continue
            seen.add(key)
            if name == "ccpaAgreement" and r.get("option"):
                label = r["option"]
            results.append({"field_key": key, "label": label, "field_type": ftype,
                            "required": bool(r.get("required")), "options": [], "description": ""})
            saw_salary = saw_salary or key == "salary"
            continue

        if tag == "select" and not name and saw_salary and "salary_period" not in seen:
            # The unnamed period dropdown right after Desired Salary.
            seen.add("salary_period")
            results.append({"field_key": "salary_period", "label": "Desired salary period", "field_type": "select",
                            "required": bool(r.get("required")), "options": list(r.get("options") or []), "description": ""})
            continue

        if not name or name in seen:
            continue
        seen.add(name)
        if tag == "select":
            ftype = "select"
        elif tag == "textarea":
            ftype = "textarea"
        elif typ == "file":
            ftype = "file"
        elif typ == "checkbox":
            ftype = "boolean"
        else:
            ftype = "text"
        results.append({"field_key": name, "label": question or name, "field_type": ftype,
                        "required": bool(r.get("required")), "options": list(r.get("options") or []) if ftype == "select" else [],
                        "description": ""})
    return results


def _fetch_breezy_questions(job_external_id: str, slug: str) -> list[dict[str, Any]]:
    """The apply page is keyed by the posting URL; the feed gives it to us."""
    if not job_external_id or not slug:
        return []
    from app.fetchers.breezy import BreezyFetcher

    raw = BreezyFetcher().fetch_one(slug, job_external_id)
    if not raw or not raw.get("url"):
        return []
    return normalise_breezy_rows(breezy_form_rows(raw["url"]))


# ---------------------------------------------------------------------------
# Personio — F378, read from the rendered apply page
# ---------------------------------------------------------------------------
# Verified on greenbone-ag.jobs.personio.com/job/{id}/apply. Next.js form,
# no captcha; every control has a stable name and an id of
# ``field-{name}`` with a proper <label for>. Required is NOT the
# attribute — it is the "* (required)" suffix on the label. Fixed names:
# first_name, last_name, email, phone, salary_expectations; custom
# questions are ``custom_attribute_{id}`` (text or a <select> whose first
# option is "Please select"); documents are file inputs named
# ``documents.cv`` / ``documents.cover-letter`` / ``documents.other``.

_PERSONIO_FORM_READY = "input[name='email']"

_PERSONIO_STRUCT_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const rows = [];
  for (const e of document.querySelectorAll('input,textarea,select')) {
    if (e.type === 'hidden' || !e.name) continue;
    const lab = e.id ? document.querySelector(`label[for="${CSS.escape(e.id)}"]`) : null;
    const label = norm(lab ? lab.innerText : (e.getAttribute('aria-label') || ''));
    rows.push({ tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name, id: e.id || '', label,
                required: !!e.required || /\(required\)|\*/.test(label),
                options: e.tagName === 'SELECT' ? [...e.options].map(o => norm(o.textContent)).filter(o => o && !/^(please select|bitte auswählen)$/i.test(o)) : [] });
  }
  return rows;
})()
"""


def personio_form_rows(apply_url: str) -> list[dict[str, Any]]:
    from app.services.playwright_browser import BrowserSession

    async def _go():
        async with BrowserSession() as session:
            await session.navigate(apply_url, wait_until="domcontentloaded", wait_for_selector=_PERSONIO_FORM_READY)
            return await session.eval_js(_PERSONIO_STRUCT_JS) or []

    return _run_in_fresh_loop(_go())


_PERSONIO_FIXED: dict[str, tuple[str, str, str]] = {
    "first_name": ("first_name", "First Name", "text"),
    "last_name": ("last_name", "Last Name", "text"),
    "email": ("email", "Email", "text"),
    "phone": ("phone", "Phone", "text"),
    "salary_expectations": ("salary_expectations", "Expected salary", "text"),
    "documents.cv": ("resume", "Resume / CV", "file"),
    "documents.cover-letter": ("cover_letter_file", "Cover Letter", "file"),
}


def _clean_personio_label(label: str) -> str:
    return re.sub(r"\s*\*?\s*\((?:required|erforderlich)\)\s*$|\s*\*\s*$", "", label or "").strip()


def normalise_personio_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        name, tag, typ = r.get("name") or "", r.get("tag"), (r.get("type") or "").lower()
        if name == "documents.other" or not name:
            continue
        label = _clean_personio_label(r.get("label") or "")
        if name in _PERSONIO_FIXED:
            key, default_label, ftype = _PERSONIO_FIXED[name]
            if key in seen:
                continue
            seen.add(key)
            results.append({"field_key": key, "label": label if key not in ("first_name", "last_name") else default_label,
                            "field_type": ftype, "required": bool(r.get("required")), "options": [], "description": ""})
            continue
        if name in seen:
            continue
        seen.add(name)
        ftype = "select" if tag == "select" else "textarea" if tag == "textarea" else "file" if typ == "file" else "boolean" if typ == "checkbox" else "text"
        results.append({"field_key": name, "label": label or name, "field_type": ftype, "required": bool(r.get("required")),
                        "options": list(r.get("options") or []) if ftype == "select" else [], "description": ""})
    return results


def _fetch_personio_questions(job_external_id: str, slug: str) -> list[dict[str, Any]]:
    if not job_external_id or not slug:
        return []
    from app.fetchers.personio import PersonioFetcher

    raw = PersonioFetcher().fetch_one(slug, job_external_id)
    if not raw:
        return []
    # ?language=en: the page otherwise renders in the tenant's default
    # language, and the answer book matches on English labels.
    return normalise_personio_rows(personio_form_rows(raw["url"].rstrip("/") + "/apply?language=en"))


# ---------------------------------------------------------------------------
# Rippling — F379, read from the rendered apply page
# ---------------------------------------------------------------------------
# Verified on ats.rippling.com/athennian/jobs/{uuid}/apply. React form;
# input ``name``s are random per render and ids are positional, but every
# control carries ``data-testid="input-{key}"`` (first_name, last_name,
# email, phone_number, current_company, linkedin_link; the résumé and
# cover-letter file inputs sit in ``[data-testid=resume|cover_letter]``)
# and ``aria-labelledby`` → the visible label; required is
# ``aria-required`` or a trailing ``*`` span. Custom questions are radio
# groups named ``customQuestions.{qid}.{optionId}`` whose question is the
# <p> that precedes the field block; ``sms_opt_in`` is such a group too.
# Comboboxes (pronouns, location) are ``input[role=combobox]`` and are
# keyed from their label. Cloudflare Turnstile is loaded INVISIBLY (the
# token is minted on submit; no widget) — the Ashby-v3 situation.

_RIPPLING_FORM_READY = '[data-testid="input-email"]'

_RIPPLING_STRUCT_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const byIds = ids => norm((ids || '').split(/\s+/).map(i => document.getElementById(i)?.innerText || '').join(' '));
  const rows = [];
  for (const e of document.querySelectorAll('input,textarea,select')) {
    if (e.type === 'hidden') continue;
    const tid = e.getAttribute('data-testid') || '';
    let key = tid.startsWith('input-') ? tid.slice(6) : '';
    let label = byIds(e.getAttribute('aria-labelledby')) || (e.labels && e.labels[0] ? norm(e.labels[0].innerText) : '') || e.getAttribute('aria-label') || e.placeholder || '';
    label = label.replace(/^Total \d+ file selected\s*/i, '');
    const field = e.closest('[data-testid="field"]') || e.closest('[role=radiogroup]')?.closest('[data-testid="field"]');
    const star = !!(field && [...field.querySelectorAll('span')].some(s => norm(s.innerText) === '*'));
    let question = '';
    if (e.type === 'radio') {
      let n = field ? field.parentElement : e.parentElement;
      for (let i = 0; i < 4 && n && !question; i++) { const p = n.querySelector(':scope > * p, :scope > p'); if (p) question = norm(p.innerText); n = n.parentElement; }
    }
    if (e.type === 'file') { const wrap = e.closest('[data-testid]'); key = wrap ? wrap.getAttribute('data-testid') : key; }
    rows.push({ tag: e.tagName.toLowerCase(), type: e.type || '', key, name: e.name || '', label, question,
                role: e.getAttribute('role') || '', required: !!(e.getAttribute('aria-required') === 'true' || e.required || star),
                value: e.value || '', option: e.type === 'radio' ? (byIds(e.getAttribute('aria-labelledby')) || e.value) : '' });
  }
  return rows;
})()
"""

_RIPPLING_FIXED: dict[str, tuple[str, str]] = {
    "first_name": ("first_name", "First name"),
    "last_name": ("last_name", "Last name"),
    "email": ("email", "Email"),
    "phone_number": ("phone", "Phone number"),
    "current_company": ("current_company", "Current company"),
    "linkedin_link": ("linkedin_url", "LinkedIn Link"),
}


def rippling_form_rows(apply_url: str) -> list[dict[str, Any]]:
    from app.services.playwright_browser import BrowserSession

    async def _go():
        async with BrowserSession() as session:
            await session.navigate(apply_url, wait_until="domcontentloaded", wait_for_selector=_RIPPLING_FORM_READY)
            await asyncio_sleep(3.0)
            return await session.eval_js(_RIPPLING_STRUCT_JS) or []

    return _run_in_fresh_loop(_go())


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def normalise_rippling_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        typ, key, name, label = (r.get("type") or "").lower(), r.get("key") or "", r.get("name") or "", r.get("label") or ""
        if typ == "file":
            fk = "resume" if key == "resume" else "cover_letter_file" if key == "cover_letter" else ""
            if fk and fk not in seen:
                seen.add(fk)
                results.append({"field_key": fk, "label": "Resume / CV" if fk == "resume" else "Cover Letter", "field_type": "file",
                                "required": bool(r.get("required")), "options": [], "description": ""})
            continue
        if typ == "radio":
            g = groups.get(name)
            if g is None:
                q = r.get("question") or ("Consent to SMS updates" if name == "sms_opt_in" else name)
                g = groups[name] = {"field_key": name, "label": q, "field_type": "select", "required": bool(r.get("required")),
                                    "options": [], "description": ""}
                results.append(g)
            opt = r.get("option") or ""
            if opt and opt not in g["options"]:
                g["options"].append(opt)
            continue
        if key in _RIPPLING_FIXED:
            fk, default_label = _RIPPLING_FIXED[key]
        elif r.get("role") == "combobox" or key in ("select-search-input", "undefined", ""):
            # An unlabeled combobox whose only text is its placeholder
            # ("Search") is a widget helper (the phone country code), not
            # a question the candidate is asked.
            if not label or label.lower() == "search":
                continue
            fk, default_label = "rippling_" + _normalise_field_key(label), label
        else:
            fk, default_label = "rippling_" + _normalise_field_key(key), label or key
        if fk in seen:
            continue
        seen.add(fk)
        results.append({"field_key": fk, "label": label or default_label,
                        "field_type": "textarea" if r.get("tag") == "textarea" else "text",
                        "required": bool(r.get("required")), "options": [], "description": "",
                        **({"combobox": True} if r.get("role") == "combobox" else {})})
    return results


def _fetch_rippling_questions(job_external_id: str, slug: str) -> list[dict[str, Any]]:
    if not job_external_id or not slug:
        return []
    uuid_ = job_external_id.split("-", 1)[1] if job_external_id.startswith("rippling-") else job_external_id
    return normalise_rippling_rows(rippling_form_rows(f"https://ats.rippling.com/{slug}/jobs/{uuid_}/apply"))


# ---------------------------------------------------------------------------
# JazzHR — F380, server-rendered apply page (extraction only)
# ---------------------------------------------------------------------------
# https://{slug}.applytojob.com/apply/{code}/{Title} is plain HTML with
# ``resumator-*`` controls: resumator-firstname-value … resumator-resume-
# value (file), questionnaire questions as ``resumator-questionnaire[{id}]``
# (text / textarea / select) and checkbox questions as
# ``resumator-checkbox-{id}-{n}``. Labels are <label for>; "*" marks
# required. Every posting carries a reCAPTCHA v2 checkbox (size=normal,
# verified live on lumivero), so JazzHR is in KNOWN_HUMAN_WALLS and never
# auto-submitted — the form is read so the review queue can show it.

_JAZZHR_FIXED: dict[str, tuple[str, str, str]] = {
    "resumator-firstname-value": ("first_name", "First Name", "text"),
    "resumator-lastname-value": ("last_name", "Last Name", "text"),
    "resumator-email-value": ("email", "Email Address", "text"),
    "resumator-phone-value": ("phone", "Phone", "text"),
    "resumator-address-value": ("address", "Address", "text"),
    "resumator-city-value": ("city", "City", "text"),
    "resumator-state-value": ("state", "State", "text"),
    "resumator-postal-value": ("postcode", "Postal Code", "text"),
    "resumator-resume-value": ("resume", "Resume / CV", "file"),
    "resumator-resumetext-value": ("resume_text", "Resume (paste)", "textarea"),
    "resumator-coverletter-value": ("cover_letter", "Cover Letter", "textarea"),
    "resumator-linkedin-value": ("linkedin_url", "LinkedIn", "text"),
    "resumator-website-value": ("website", "Website", "text"),
}


def parse_jazzhr_form(html: str) -> list[dict[str, Any]]:
    """Pure: the apply page HTML → question schema."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    checkbox_groups: dict[str, dict[str, Any]] = {}

    def label_for(el) -> str:
        lab = soup.find("label", attrs={"for": el.get("id")}) if el.get("id") else None
        text = " ".join(lab.get_text(" ", strip=True).split()) if lab else ""
        return text

    for el in soup.select("input, textarea, select"):
        name = el.get("name") or ""
        typ = (el.get("type") or "").lower()
        if not name or typ in ("hidden", "submit", "button") or name in ("g-recaptcha-response", "resumator-xml-value"):
            continue
        label = label_for(el)
        required = label.rstrip().endswith("*") or el.has_attr("required")
        clean = label.rstrip("* ").strip()
        if name in _JAZZHR_FIXED:
            key, default, ftype = _JAZZHR_FIXED[name]
            if key in seen:
                continue
            seen.add(key)
            results.append({"field_key": key, "label": clean or default, "field_type": ftype, "required": required,
                            "options": [], "description": ""})
            continue
        m = re.match(r"resumator-checkbox-(\d+)-\d+$", name)
        if m:
            gid = f"resumator-checkbox-{m.group(1)}"
            g = checkbox_groups.get(gid)
            if g is None:
                # The question text sits on the group's heading label, found by
                # the nearest preceding <label> that is not an option label.
                q = ""
                prev = el.find_previous("label")
                while prev is not None and prev.get("for", "").startswith("resumator-checkbox-"):
                    prev = prev.find_previous("label")
                if prev is not None:
                    q = " ".join(prev.get_text(" ", strip=True).split())
                g = checkbox_groups[gid] = {"field_key": gid, "label": q.rstrip("* ").strip() or gid, "field_type": "multi_select",
                                            "required": q.rstrip().endswith("*"), "options": [], "description": ""}
                results.append(g)
            if clean and clean not in g["options"]:
                g["options"].append(clean)
            continue
        if name in seen:
            continue
        seen.add(name)
        if el.name == "select":
            ftype = "select"
            options = [" ".join(o.get_text(" ", strip=True).split()) for o in el.select("option")]
            options = [o for o in options if o and not re.match(r"^(please select|select|--)", o, re.I)]
        else:
            ftype = "textarea" if el.name == "textarea" else "file" if typ == "file" else "boolean" if typ == "checkbox" else "text"
            options = []
        results.append({"field_key": name, "label": clean or name, "field_type": ftype, "required": required,
                        "options": options, "description": ""})
    return results


def _fetch_jazzhr_questions(job_external_id: str, slug: str) -> list[dict[str, Any]]:
    if not job_external_id or not slug:
        return []
    import httpx

    code = job_external_id.split("-", 1)[1] if job_external_id.startswith("jazzhr-") else job_external_id
    url = f"https://{slug}.applytojob.com/apply/{code}/"
    with httpx.Client(timeout=25, follow_redirects=True, headers=_BROWSER_UA) as client:
        resp = client.get(url)
        resp.raise_for_status()
        if "applytojob.com" not in str(resp.url):
            return []
        return parse_jazzhr_form(resp.text)


# ---------------------------------------------------------------------------
# Teamtailor — F381, read from the rendered application form
# ---------------------------------------------------------------------------
# {job url}/applications/new renders a Rails form (#job-application-form,
# no captcha; verified on virtasant, clearroute, xci) with candidate[…]
# names: first_name, last_name, email, phone, location[query],
# work_history, job_applications_attributes[0][cover_letter], the résumé
# as #candidate_resume_remote_url (file), consent checkboxes, location
# checkboxes candidate[location_ids][], and custom answers
# candidate[answers_attributes][N][text|number|range|boolean|choice].
# Labels are <label for>; "* Required" marks required; choice groups sit
# in a <fieldset> whose <legend> is the question. The phone widget's
# search input (iti-*) and cookie-banner checkboxes are not questions.

_TEAMTAILOR_FORM_READY = "#job-application-form input[name='candidate[email]']"

_TEAMTAILOR_STRUCT_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const form = document.getElementById('job-application-form');
  if (!form) return [];
  const rows = [];
  for (const e of form.querySelectorAll('input,textarea,select')) {
    if (e.type === 'hidden' || e.type === 'submit' || /^iti-/.test(e.id || '') || e.name === 'range-custom_number') continue;
    const lab = e.id ? form.querySelector(`label[for="${CSS.escape(e.id)}"]`) : null;
    const label = norm(lab ? lab.innerText : '');
    const fs = e.closest('fieldset');
    const legend = fs ? norm(fs.querySelector('legend')?.innerText || '') : '';
    rows.push({ tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', id: e.id || '', label, legend,
                required: !!(e.required || /\*\s*required|^required\./i.test(label) || /\*\s*required/i.test(legend)),
                min: e.min || '', max: e.max || '',
                options: e.tagName === 'SELECT' ? [...e.options].map(o => norm(o.textContent)).filter(Boolean) : [] });
  }
  return rows;
})()
"""

_TEAMTAILOR_FIXED: dict[str, tuple[str, str, str]] = {
    "candidate[first_name]": ("first_name", "First name", "text"),
    "candidate[last_name]": ("last_name", "Last name", "text"),
    "candidate[email]": ("email", "Email", "text"),
    "candidate[phone]": ("phone", "Phone", "text"),
    "candidate[location][query]": ("address", "Address", "text"),
    "candidate[work_history]": ("work_history", "Work history", "textarea"),
    "candidate[job_applications_attributes][0][cover_letter]": ("cover_letter", "Cover letter", "textarea"),
    "candidate[consent_given]": ("privacy_consent", "I agree that I have read the Privacy Policy", "boolean"),
    "candidate[consent_given_future_jobs]": ("future_jobs_consent", "May contact me about future job opportunities", "boolean"),
}


def _tt_clean(label: str) -> str:
    return re.sub(r"\s*\*\s*required\.?\s*$|^required\.\s*", "", label or "", flags=re.I).strip()


def teamtailor_form_rows(apply_url: str) -> list[dict[str, Any]]:
    from app.services.playwright_browser import BrowserSession

    async def _go():
        async with BrowserSession() as session:
            await session.navigate(apply_url, wait_until="domcontentloaded", wait_for_selector=_TEAMTAILOR_FORM_READY)
            # The file inputs are mounted by the uploader script after load.
            await asyncio_sleep(3.0)
            return await session.eval_js(_TEAMTAILOR_STRUCT_JS) or []

    return _run_in_fresh_loop(_go())


def normalise_teamtailor_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        name, tag, typ = r.get("name") or "", r.get("tag"), (r.get("type") or "").lower()
        rid, label, legend = r.get("id") or "", _tt_clean(r.get("label") or ""), _tt_clean(r.get("legend") or "")
        if typ == "file":
            key = "resume" if rid == "candidate_resume_remote_url" else "additional_files" if rid == "candidate_file_remote_url" else ""
            if key == "resume" and key not in seen:
                seen.add(key)
                results.append({"field_key": key, "label": label or "Upload CV", "field_type": "file",
                                "required": bool(r.get("required")), "options": [], "description": ""})
            continue
        if not name:
            continue
        if typ in ("radio", "checkbox") and (name.endswith("[choice]") or name.endswith("[boolean]") or name == "candidate[location_ids][]"):
            g = groups.get(name)
            if g is None:
                q = legend or ("Locations" if name == "candidate[location_ids][]" else name)
                g = groups[name] = {"field_key": name, "label": q, "field_type": "multi_select" if typ == "checkbox" else "select",
                                    "required": bool(r.get("required")), "options": [], "description": ""}
                results.append(g)
            if label and label not in g["options"]:
                g["options"].append(label)
            continue
        if name in _TEAMTAILOR_FIXED:
            key, default, ftype = _TEAMTAILOR_FIXED[name]
            if key in seen:
                continue
            seen.add(key)
            results.append({"field_key": key, "label": label or default, "field_type": ftype,
                            "required": bool(r.get("required")), "options": [], "description": ""})
            continue
        if name in seen:
            continue
        seen.add(name)
        if tag == "select":
            ftype = "select"
        elif tag == "textarea":
            ftype = "textarea"
        elif typ == "checkbox":
            ftype = "boolean"
        else:
            ftype = "text"
        desc = f"Number between {r.get('min')} and {r.get('max')}" if typ == "range" and r.get("min") else ("Number" if typ == "number" else "")
        results.append({"field_key": name, "label": label or legend or name, "field_type": ftype,
                        "required": bool(r.get("required")), "options": list(r.get("options") or []) if ftype == "select" else [],
                        "description": desc})
    return results


def _fetch_teamtailor_questions(job_external_id: str, slug: str) -> list[dict[str, Any]]:
    if not job_external_id or not slug:
        return []
    from app.fetchers.teamtailor import TeamtailorFetcher

    raw = TeamtailorFetcher().fetch_one(slug, job_external_id)
    if not raw:
        return []
    return normalise_teamtailor_rows(teamtailor_form_rows(raw["url"].rstrip("/") + "/applications/new"))


# ---------------------------------------------------------------------------
# Pinpoint — F387, read from the rendered application form
# ---------------------------------------------------------------------------
# The posting page shows the form only after "Apply" is clicked; it is a
# Rails form (no captcha, no shadow DOM) with fixed
# ``application_form[application][...]`` names and custom questions as
# ``application_form[application][answers_attributes][N][boolean_answer |
# text_answer]``. Each question also carries hidden ``[N][title]`` /
# ``[N][question_type]`` inputs — the title is the label we show.
# Select-type questions (and Country / State / the equality-monitoring
# block) are React dropdowns whose mobile fallback is a nameless native
# <select>; setting that select with React's value setter drives the
# React state and the hidden value that posts (verified on made-tech).
# ``_PINPOINT_SELECT_KEY_JS`` maps such a select's id to a stable
# field_key shared by the extractor and the submitter.

_PINPOINT_SELECT_KEY_JS = r"""
const nearLabel = (s) => { let n = s.parentElement; for (let i = 0; i < 4 && n; i++) { const l = n.querySelector(':scope > label'); if (l) return (l.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase(); n = n.parentElement; } return ''; };
const keyForSelect = (s) => {
  const id = s.id || '';
  if (id === 'application_form[application][country]') return 'country';
  if (id === 'application_form[application][state]' || nearLabel(s) === 'state' || nearLabel(s) === 'state / province') return 'state';
  let m = id.match(/answers_attributes_(\d+)_mobile_select$/);
  if (m) return 'application_form[application][answers_attributes][' + m[1] + '][choice]';
  if (id.startsWith('application_form_equality_monitoring_')) return 'equality_monitoring[' + id.slice('application_form_equality_monitoring_'.length) + ']';
  return '';
};
"""

_PINPOINT_STRUCT_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const form = document.querySelector('form');
  if (!form) return [];
  """ + _PINPOINT_SELECT_KEY_JS + r"""
  // A usable question label is specific, not a numbered section header
  // ("1.Personal Details", "3.Questions", "Diversity and Inclusion …").
  const usable = t => { const s = norm(t); return s && !/^\d+\.|^(yes|no|select\.\.\.)$/i.test(s) && !/^(diversity and inclusion|personal details|questions)\b/i.test(s) && s.length < 200; };
  const labelText = (l) => { const t = l.querySelector('.external-form__label--title'); return norm(t ? t.innerText : l.innerText); };
  const question = (el) => {
    const own = el.id && form.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (own && usable(labelText(own))) return labelText(own);
    let sib = el.previousElementSibling;
    for (let i = 0; i < 3 && sib; i++) { const l = sib.matches('label,h3,h4,p,strong') ? sib : sib.querySelector('label,h3,h4,strong'); if (l && usable(labelText(l))) return labelText(l); sib = sib.previousElementSibling; }
    let n = el.parentElement;
    for (let i = 0; i < 5 && n; i++) {
      for (const h of n.querySelectorAll(':scope > label, :scope > legend, :scope > p, :scope > h3, :scope > h4, :scope > strong')) {
        if (usable(labelText(h))) return labelText(h);
      }
      n = n.parentElement;
    }
    return '';
  };
  const requiredMark = (el) => {
    let n = el.parentElement;
    for (let i = 0; i < 5 && n; i++) { if (n.querySelector(':scope > label.external-form__label--required, :scope > label .external-form__label--required')) return true; n = n.parentElement; }
    return false;
  };
  const rows = [];
  for (const e of form.querySelectorAll('input,select,textarea')) {
    if (e.type === 'submit') continue;
    if (e.type === 'hidden') { if (/answers_attributes\]\[\d+\]\[(title|question_type)\]$/.test(e.name)) rows.push({ tag: 'hidden', type: 'hidden', name: e.name, value: e.value }); continue; }
    const lab = e.id ? form.querySelector(`label[for="${CSS.escape(e.id)}"]`) : null;
    rows.push({ tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', key: e.tagName === 'SELECT' && !e.name ? keyForSelect(e) : '',
                label: norm(lab ? labelText(lab) : ''), question: question(e),
                required: !!(e.required || e.getAttribute('aria-required') === 'true' || requiredMark(e)),
                options: e.tagName === 'SELECT' ? [...e.options].filter(o => o.value && !/^select\.\.\./i.test(norm(o.textContent))).map(o => norm(o.textContent)) : [] });
  }
  return rows;
})()
"""

_PINPOINT_FIXED: dict[str, tuple[str, str, str]] = {
    "first_name": ("first_name", "First name", "text"),
    "last_name": ("last_name", "Last name", "text"),
    "preferred_name": ("preferred_name", "Preferred name", "text"),
    "email": ("email", "Email Address", "text"),
    "phone": ("phone", "Phone", "text"),
    "address1": ("address", "Address", "text"),
    "town": ("city", "Town / City", "text"),
    "postcode": ("postcode", "Postcode", "text"),
    "cv": ("resume", "Résumé / CV", "file"),
    "summary": ("summary", "Personal Summary", "textarea"),
}
_PINPOINT_SELECT_LABELS: dict[str, str] = {"country": "Country", "state": "State"}
_PINPOINT_FIELD_RE = re.compile(r"application_form\[application\]\[([a-z0-9_]+)\]")
_PINPOINT_ANSWER_RE = re.compile(r"answers_attributes\]\[(\d+)\]\[(boolean_answer|text_answer|choice)\]")
_PINPOINT_HIDDEN_RE = re.compile(r"answers_attributes\]\[(\d+)\]\[(title|question_type)\]$")


def pinpoint_form_rows(posting_url: str) -> list[dict[str, Any]]:
    from app.services.playwright_browser import BrowserSession

    async def _go():
        async with BrowserSession() as session:
            await session.navigate(posting_url, wait_until="domcontentloaded", wait_for_selector="body")
            await asyncio_sleep(2.0)
            try:
                await session.click('a:has-text("Apply"), button:has-text("Apply")')
            except Exception:
                pass
            await asyncio_sleep(3.0)
            return await session.eval_js(_PINPOINT_STRUCT_JS) or []

    return _run_in_fresh_loop(_go())


def normalise_pinpoint_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    answers: dict[str, dict[str, Any]] = {}
    titles: dict[str, str] = {}
    for r in rows:
        hm = _PINPOINT_HIDDEN_RE.search(r.get("name") or "") if r.get("tag") == "hidden" else None
        if hm and hm.group(2) == "title" and (r.get("value") or "").strip():
            titles[hm.group(1)] = (r.get("value") or "").strip()
    for r in rows:
        if r.get("tag") == "hidden":
            continue
        name, tag, typ = r.get("name") or "", r.get("tag"), (r.get("type") or "").lower()
        key = r.get("key") or ""
        m = _PINPOINT_FIELD_RE.match(name)
        am = _PINPOINT_ANSWER_RE.search(name or key)
        if am:
            idx, kind = am.group(1), am.group(2)
            g = answers.get(idx)
            if g is None:
                base = (name or key).split(f"[{kind}]")[0]
                g = answers[idx] = {"field_key": f"{base}[{kind}]",
                                    "label": titles.get(idx) or r.get("question") or f"Question {idx}",
                                    "field_type": "boolean" if kind == "boolean_answer" else ("select" if kind == "choice" else "textarea"),
                                    "required": bool(r.get("required")), "options": list(r.get("options") or []), "description": ""}
                results.append(g)
            g["required"] = g["required"] or bool(r.get("required"))
            continue
        if m and m.group(1) in _PINPOINT_FIXED:
            fk, label, ftype = _PINPOINT_FIXED[m.group(1)]
            if fk in seen:
                continue
            seen.add(fk)
            results.append({"field_key": fk, "label": r.get("label") or label, "field_type": ftype,
                            "required": bool(r.get("required")), "options": [], "description": ""})
            continue
        if tag == "select" and not name:
            if key:
                label = _PINPOINT_SELECT_LABELS.get(key) or (key[len("equality_monitoring["):-1] if key.startswith("equality_monitoring[") else "") or r.get("question") or key
            else:
                # An enhanced dropdown we can't address — surfaced so the
                # gate asks; the submitter reports it unplaceable.
                if not r.get("question"):
                    continue
                key = "pinpoint_" + _normalise_field_key(r.get("question"))
                label = r.get("question")
            if key in seen:
                continue
            seen.add(key)
            # State is rendered only for some countries (seen for the US,
            # absent for the UK), so it must never block a submission.
            conditional = key == "state"
            results.append({"field_key": key, "label": label, "field_type": "select",
                            "required": bool(r.get("required")) and not conditional, "options": list(r.get("options") or []),
                            "description": "Only asked for some countries." if conditional else ""})
    return results


def _fetch_pinpoint_questions(job_external_id: str, slug: str) -> list[dict[str, Any]]:
    if not job_external_id or not slug:
        return []
    from app.fetchers.pinpoint import PinpointFetcher

    raw = PinpointFetcher().fetch_one(slug, job_external_id)
    if not raw or not raw.get("url"):
        return []
    return normalise_pinpoint_rows(pinpoint_form_rows(raw["url"]))


