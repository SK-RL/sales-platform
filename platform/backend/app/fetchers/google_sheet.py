"""Google Sheets fetcher — team-curated job lists as a scan source.

The sales team maintains Google Sheets (updated daily) listing jobs
from sources our scrapers don't cover. This fetcher treats each sheet
as a job board: register it once via the admin Platforms page
(platform=``google_sheet``, slug = the sheet URL or ID) and the
regular scan cadence ingests new rows automatically. Rows that
already exist are updated in place — the scan pipeline's
upsert-by-``external_id`` gives "add only if it doesn't exist"
semantics for free.

── Access model ──────────────────────────────────────────────────

No Google Cloud project / service account / API key. The fetcher
reads the sheet's CSV export endpoint::

    https://docs.google.com/spreadsheets/d/{id}/export?format=csv&gid={gid}

which works for any sheet shared as **"Anyone with the link →
Viewer"**. A private sheet redirects to the Google login page — the
fetcher detects the HTML response and raises a clear error that
lands in the scan-error log ("sheet is not link-shared").

── Slug formats accepted ─────────────────────────────────────────

* Full URL:  https://docs.google.com/spreadsheets/d/<ID>/edit#gid=123
* Bare ID:   <ID>
* ID + tab:  <ID>#gid=123

``gid`` selects a specific tab; omitted → first tab (gid=0).

── Expected columns (header row, case-insensitive) ───────────────

Required (aliases accepted, see _HEADER_ALIASES):
    Title    — "title", "job title", "role", "position", "designation"
    Company  — "company", "company name", "client", "employer"

Optional:
    URL      — "url", "link", "job link", "jd link", "apply link"
    Location — "location", "city", "country", "region"
    Remote   — "remote", "work mode", "work type"
    Salary   — "salary", "ctc", "package", "compensation"
    Department, Type, Posted — see aliases.

A sheet whose header row lacks a recognisable Title or Company
column raises with the list of headers actually found — visible in
/monitoring scan errors so the team can fix the sheet, not silently
ingest nothing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from typing import Any, Optional

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

EXPORT_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

# Safety ceiling — a runaway sheet (formula spill, accidental paste)
# can't flood the scan. Real curated lists are dozens-to-hundreds.
_MAX_ROWS = 5000

# Canonical field → accepted header spellings (lowercased, stripped),
# in PRIORITY ORDER: ``_map_headers`` walks aliases first and columns
# second, so an earlier alias beats a later one even when the later
# one appears in an earlier column. That ordering matters for the
# team's real sheets, where "Technology/Designation" (the job title)
# coexists with a bare "Designation" column that describes the
# *contact person* — naive column-order matching grabbed the wrong
# one.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "title": (
        "title", "job title", "job_title", "jobtitle",
        # Team-sheet convention: the role column is headed
        # "Technology/Designation" — must outrank bare "designation".
        "technology/designation", "technology / designation",
        "technology",
        "role", "position", "job role", "job",
        "designation",
    ),
    "company": (
        "company", "company name", "company_name", "companyname",
        # Team-sheet convention.
        "name of company", "company title",
        "client", "employer", "organisation", "organization", "org",
    ),
    "url": (
        "url", "link", "job link", "job_link", "job url", "job_url",
        # Team-sheet convention.
        "job post link", "job post url", "job posting link",
        "jd link", "jd_link", "jd", "apply link", "apply_link",
        "application link", "application_link", "jd url",
    ),
    "location": (
        "location", "city", "geo", "region", "country",
        "job location", "job_location", "place",
    ),
    "remote": (
        "remote", "remote scope", "remote_scope", "work mode",
        "work_mode", "workmode", "work type", "work_type", "mode",
    ),
    "salary": (
        "salary", "ctc", "package", "salary range", "salary_range",
        "compensation", "pay", "budget",
    ),
    "department": ("department", "team", "function", "category", "cluster"),
    "employment_type": (
        "employment type", "employment_type", "type", "job type",
        "job_type", "contract", "contract type",
    ),
    "posted_at": (
        "date", "posted", "posted at", "posted_at", "date posted",
        "added", "added on", "posting date",
    ),
    "notes": ("notes", "note", "remarks", "comments", "detail", "details"),
    # F354 — recruiter/HR contact columns the team curates in their
    # sheets. Capture-only at the fetcher level (they ride along in
    # raw_json.row); the nightly ``sync_sheet_contacts`` task promotes
    # them into CompanyContact rows for the company section.
    "contact_name": ("contact name", "poc", "point of contact", "recruiter name", "hr name", "name"),
    "contact_email": ("email id", "email_id", "contact email", "recruiter email", "hr email", "poc email"),
    "company_email": ("company email", "company_email", "careers email"),
    "contact_linkedin": ("linkedin", "linkedin url", "contact linkedin"),
    "contact_title": ("contact designation", "contact title", "poc designation"),
}

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_GID_RE = re.compile(r"[#&?]gid=(\d+)")


def parse_sheet_slug(slug: str) -> tuple[str, str]:
    """Extract ``(sheet_id, gid)`` from any accepted slug format.

    Raises ValueError on garbage so the failure surfaces in scan
    errors rather than producing a 404 fetch against a mangled URL.
    """
    s = (slug or "").strip()
    if not s:
        raise ValueError("empty google_sheet slug")

    m = _SHEET_ID_RE.search(s)
    sheet_id = m.group(1) if m else s.split("#")[0].split("?")[0]
    # Bare IDs are base64ish, typically 40+ chars; reject obviously
    # wrong values (spaces, slashes) early.
    if not re.fullmatch(r"[a-zA-Z0-9_-]{20,}", sheet_id):
        raise ValueError(
            f"could not extract a Google Sheet ID from slug {slug!r} — "
            "use the full sheet URL or the bare ID from it"
        )
    gid_m = _GID_RE.search(s)
    gid = gid_m.group(1) if gid_m else "0"
    return sheet_id, gid


class GoogleSheetFetcher(BaseFetcher):
    """Ingest a link-shared Google Sheet of jobs. Slug = sheet URL/ID."""

    PLATFORM = "google_sheet"

    def fetch(self, slug: str) -> list[dict]:
        sheet_id, gid = parse_sheet_slug(slug)
        url = EXPORT_URL.format(sheet_id=sheet_id, gid=gid)

        client = self._get_client()
        resp = client.get(url)

        content_type = resp.headers.get("content-type", "")
        if resp.status_code in (401, 403) or "text/html" in content_type:
            # Private sheet → Google serves the login page (HTML 200)
            # or a 401/403. Raise so the scan-error log carries an
            # actionable message instead of silently ingesting zero.
            raise ValueError(
                f"Google Sheet {sheet_id} is not link-shared. Open the "
                "sheet → Share → General access → 'Anyone with the "
                "link' → Viewer, then rescan."
            )
        resp.raise_for_status()

        text = resp.content.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return []

        header_map = self._map_headers(rows[0])
        if "title" not in header_map or "company" not in header_map:
            found = [h.strip() for h in rows[0] if h.strip()]
            raise ValueError(
                f"Google Sheet {sheet_id} header row is missing a "
                f"recognisable Title and/or Company column. Headers "
                f"found: {found!r}. Expected e.g. 'Job Title' + "
                f"'Company' (see fetcher docs for all aliases)."
            )

        jobs: list[dict] = []
        skipped = 0
        for raw_row in rows[1 : _MAX_ROWS + 1]:
            normalized = self._normalize_row(raw_row, header_map, sheet_id)
            if normalized:
                jobs.append(normalized)
            else:
                skipped += 1

        if skipped:
            logger.info(
                "google_sheet %s: ingested %d rows, skipped %d "
                "(missing title/company)",
                sheet_id, len(jobs), skipped,
            )
        if len(rows) - 1 > _MAX_ROWS:
            logger.warning(
                "google_sheet %s: sheet has %d rows, capped at %d",
                sheet_id, len(rows) - 1, _MAX_ROWS,
            )
        return jobs

    # ── internals ────────────────────────────────────────────────

    @staticmethod
    def _map_headers(header_row: list[str]) -> dict[str, int]:
        """Map canonical field names → column index.

        Aliases are walked in priority order, columns second — so
        "Technology/Designation" (listed early in the title aliases)
        beats a bare "Designation" column even when the latter sits
        further left in the sheet. See the _HEADER_ALIASES comment.
        """
        out: dict[str, int] = {}
        cleaned = [h.strip().lower() for h in header_row]
        for field, aliases in _HEADER_ALIASES.items():
            for alias in aliases:
                for idx, header in enumerate(cleaned):
                    if header == alias:
                        out[field] = idx
                        break
                if field in out:
                    break
        # F354 fixup: the team sheets carry BOTH "Technology/
        # Designation" (the job title) and a bare "Designation"
        # column describing the CONTACT person. When title resolved
        # to a different column, re-purpose the bare "designation"
        # column as the contact's title.
        if "contact_title" not in out and "designation" in cleaned:
            didx = cleaned.index("designation")
            if out.get("title") != didx:
                out["contact_title"] = didx
        return out

    def _normalize_row(
        self, row: list[str], hm: dict[str, int], sheet_id: str
    ) -> Optional[dict]:
        def cell(field: str) -> str:
            idx = hm.get(field)
            if idx is None or idx >= len(row):
                return ""
            return (row[idx] or "").strip()

        title = cell("title")
        company_name = cell("company")
        if not title or not company_name:
            return None

        url = cell("url")
        location_raw = cell("location")
        remote_cell = cell("remote")

        # Stable identity across daily scans → upsert instead of
        # duplicate. URL is the strongest key when present; fall back
        # to (company, title) so URL-less rows still dedupe.
        if url:
            key = url
        else:
            key = f"{company_name.lower()}|{title.lower()}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        external_id = f"gsheet-{sheet_id[:8]}-{digest}"

        # remote_scope feeds the geography/remote-policy classifier.
        # Prefer the explicit Remote/Work-mode column; else let the
        # base heuristic sniff the location text.
        remote_scope = remote_cell or (
            self._detect_remote_scope(location_raw, title) or ""
        )

        raw_json = {
            "sheet_id": sheet_id,
            "row": {f: cell(f) for f in _HEADER_ALIASES if hm.get(f) is not None},
        }

        return {
            "external_id": external_id,
            "company_slug": self._slugify(company_name),
            "company_name": company_name,
            "title": title,
            # URL-less rows get the sheet itself as the link target —
            # the team can locate the row; better than an empty href.
            "url": url or f"https://docs.google.com/spreadsheets/d/{sheet_id}",
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": cell("department"),
            "employment_type": cell("employment_type"),
            "salary_range": cell("salary"),
            "posted_at": cell("posted_at") or None,
            "raw_json": raw_json,
        }

    @staticmethod
    def _slugify(name: str) -> str:
        """Same conservative shape as AdzunaFetcher._slugify so the
        same employer arriving from a sheet and a scan lands on one
        Company row."""
        s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return s[:200] or "sheet-company"
