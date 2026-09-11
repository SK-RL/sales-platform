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
    {"greenhouse", "recruitee", "lever", "workable", "ashby"}
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
