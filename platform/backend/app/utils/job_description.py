"""Shared helpers for extracting a Job's description from upstream raw_json.

Regression finding 97: `JobDescription.text_content` was empty for the
majority of rows in prod. Resume ATS scores collapsed into 4 distinct
values across 600+ rows because `_extract_job_keywords` received
`description_text=""` and fell back to the role-cluster keyword baseline
— so every infra job produced the same 18 matched/missing lists and a
byte-identical score.

The root cause: no step in the scan pipeline ever populated
`JobDescription`. The fetchers dumped the whole upstream payload into
`Job.raw_json`, and `_upsert_job` never looked at the description fields.
`GET /jobs/{id}/description` had its own ad-hoc fallback against
`raw_json`, but the scoring task read only from `JobDescription` and
therefore saw nothing.

This module centralises the per-platform key mapping so both the scan
pipeline (prospective fix — writes to `JobDescription` on every upsert)
and the scoring task (retroactive fallback — reads directly from
`Job.raw_json` when the row is missing) agree on where the text lives.
"""

from __future__ import annotations

import html as html_mod
import re

from bs4 import BeautifulSoup


# Keys that contain HTML description content, keyed by the ATS platform
# name. Order within each list is priority — first non-empty wins. Where a
# platform has both a plain-text and an HTML field available, prefer the
# HTML (we can always strip, but we can't un-strip).
_HTML_KEYS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "greenhouse": ("content",),  # HTML-escaped by Greenhouse
    "lever": ("description", "descriptionBody"),
    "ashby": ("descriptionHtml", "description"),
    "workable": ("full_description", "description"),
    "bamboohr": ("jobOpeningDescription",),
    "jobvite": ("description", "summary"),
    "recruitee": ("description",),
    "wellfound": ("description",),
    "himalayas": ("description",),
    "smartrecruiters": ("description",),  # fallback — real data lives in jobAd.sections
    "career_page": ("description", "description_html"),
    # F332: WWR feeds RSS ``<description>`` content as HTML; the
    # fetcher persists it under ``raw_json["description"]``.
    # Listed explicitly so a future raw_json key rename in WWR
    # doesn't silently fall through to the default fallback and
    # quietly drop description coverage again.
    "weworkremotely": ("description",),
}

# Keys that contain plain-text description. When present, we skip the HTML
# path entirely (no need to strip tags). `descriptionPlain` is the Lever /
# Ashby convention.
_TEXT_KEYS: tuple[str, ...] = (
    "descriptionPlain",
    "description_text",
    "description_plain",
    "plain_description",
)

# Supplementary fields that, if present, get appended to the main
# description. Lever and Ashby break requirements / nice-to-have into
# separate "lists" or "additional" blobs; merging them gives the keyword
# scorer the full corpus.
_SUPPLEMENTARY_KEYS: tuple[str, ...] = (
    "additional",
    "additionalPlain",
)


def _html_to_text(html: str) -> str:
    """Strip HTML to plain text. Used when upstream only gives us HTML."""
    if not html:
        return ""
    # Some upstreams (Greenhouse) double-encode: the JSON field contains
    # HTML-escaped HTML, i.e. `&lt;p&gt;hello&lt;/p&gt;`. Unescape once so
    # BeautifulSoup sees real tags.
    if "&lt;" in html:
        html = html_mod.unescape(html)
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    # Collapse consecutive blank lines + strip per-line whitespace.
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _smartrecruiters_sections(raw_json: dict) -> str:
    """Extract the concatenated HTML from SmartRecruiters `jobAd.sections`.

    SmartRecruiters' posting listing sometimes returns a `jobAd.sections`
    object with `companyDescription`, `jobDescription`, `qualifications`,
    `additionalInformation` — each a dict with `text` (HTML). We
    concatenate in the order a human would read them.
    """
    job_ad = raw_json.get("jobAd")
    if not isinstance(job_ad, dict):
        return ""
    sections = job_ad.get("sections")
    if not isinstance(sections, dict):
        return ""

    parts: list[str] = []
    for name in (
        "companyDescription",
        "jobDescription",
        "qualifications",
        "additionalInformation",
    ):
        section = sections.get(name)
        if isinstance(section, dict):
            text = section.get("text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text)

    return "\n\n".join(parts)


def extract_description(platform: str, raw_json: dict | None) -> tuple[str, str]:
    """Return ``(html_content, text_content)`` from an upstream raw_json blob.

    Either may be empty. If the upstream only provides HTML, the text is
    derived by stripping tags; if it only provides plain text, the HTML
    field is left empty (we don't fabricate markup).

    This function is intentionally conservative — it only reads well-known
    keys per platform. Unknown shapes return ``("", "")`` rather than
    guessing, because a wrong guess could feed noise (navigation chrome,
    cookie banners) into the keyword scorer.
    """
    if not isinstance(raw_json, dict):
        return "", ""

    # 1. Plain-text path first — if upstream gave us text, we don't need
    #    to round-trip through BeautifulSoup.
    text = ""
    for key in _TEXT_KEYS:
        v = raw_json.get(key)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break

    # 2. HTML path — per-platform key priority.
    html = ""
    keys = _HTML_KEYS_BY_PLATFORM.get(
        platform,
        # Sensible fallback for platforms without an explicit mapping
        # (including `career_page` and anything new): check the two most
        # common fields.
        ("description", "content"),
    )
    for key in keys:
        v = raw_json.get(key)
        if isinstance(v, str) and v.strip():
            html = v
            break

    # 3. SmartRecruiters — merge structured sections if we have them.
    #    These take precedence over the flat `description` fallback
    #    because the sections contain the real requirements body.
    if platform == "smartrecruiters":
        sr_html = _smartrecruiters_sections(raw_json)
        if sr_html:
            html = sr_html

    # 4. Supplementary blobs (Lever-style). Append if longer than nothing
    #    — they contain the requirements list which is the richest
    #    keyword source.
    for key in _SUPPLEMENTARY_KEYS:
        extra = raw_json.get(key)
        if isinstance(extra, str) and extra.strip():
            html = f"{html}\n\n{extra}" if html else extra

    # 5. Derive text from HTML if we only got HTML.
    if html and not text:
        text = _html_to_text(html)

    # 6. Belt-and-suspenders — some upstreams double-encode HTML. If the
    #    "HTML" field is plain text with escaped angle brackets, unescape
    #    it so downstream consumers (frontend sanitizer, sanitize_html)
    #    see real markup.
    if html and "&lt;" in html and "<" not in html:
        html = html_mod.unescape(html)

    return html, text
