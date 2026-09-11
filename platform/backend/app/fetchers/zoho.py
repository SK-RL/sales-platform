"""Fetch open positions from Zoho Recruit career sites.

F392. ``https://{slug}.zohorecruit.com/jobs/Careers`` is a JS app, but
the server-rendered page embeds the published openings as JSON:
``[{"Job_Opening_Name", "Posting_Title", "id", "Remote_Job", "Job_Type",
"Country", "City", "Publish", ...}]`` (verified on siliconcedars: 8
openings). A posting lives at ``/jobs/Careers/{id}/{slug-title}``.
Applying goes through an "I'm interested" form that ends in an image
CAPTCHA ("Type below image text"), so Zoho is scan + link only — see
``fetchers.questions.KNOWN_HUMAN_WALLS``.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

BOARD_URL = "https://{slug}.zohorecruit.com/jobs/Careers"
POSTING_URL = "https://{slug}.zohorecruit.com/jobs/Careers/{job_id}/{title_slug}"
_OPENING_RE = re.compile(r'\{[^{}]*"Posting_Title"[^{}]*\}')


def _title_slug(title: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9]+", "-", title or "")).strip("-") or "job"


class ZohoRecruitFetcher(BaseFetcher):
    PLATFORM = "zoho"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            resp = client.get(BOARD_URL.format(slug=slug))
        except httpx.RequestError as exc:
            logger.warning("Zoho Recruit %s request failed: %s", slug, exc)
            return []
        if resp.status_code != 200:
            logger.info("Zoho Recruit %s returned %s", slug, resp.status_code)
            return []
        jobs = self.parse_board(resp.text, slug)
        logger.info("Zoho Recruit %s fetched %d openings", slug, len(jobs))
        return jobs

    def parse_board(self, page: str, slug: str) -> list[dict]:
        text = html_lib.unescape(page or "")
        jobs: list[dict] = []
        seen: set[str] = set()
        for m in _OPENING_RE.finditer(text):
            try:
                raw = json.loads(m.group(0))
            except ValueError:
                continue
            if not isinstance(raw, dict) or raw.get("Publish") is False:
                continue
            j = self._normalize(raw, slug)
            if j and j["external_id"] not in seen:
                seen.add(j["external_id"])
                jobs.append(j)
        return jobs

    def _normalize(self, raw: dict[str, Any], slug: str) -> dict | None:
        job_id = str(raw.get("id") or "")
        title = (raw.get("Posting_Title") or raw.get("Job_Opening_Name") or "").strip()
        if not job_id.isdigit() or not title:
            return None
        city, country = (raw.get("City") or "").strip(), (raw.get("Country") or "").strip()
        location_raw = ", ".join(p for p in (city, country) if p)
        remote = bool(raw.get("Remote_Job"))
        if remote:
            location_raw = "Remote" + (f" ({location_raw})" if location_raw else "")
        return {
            "external_id": f"zoho-{job_id}",
            "company_slug": slug,
            "title": title,
            "url": POSTING_URL.format(slug=slug, job_id=job_id, title_slug=_title_slug(title)),
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": "remote" if remote else (self._detect_remote_scope(location_raw, title) or ""),
            "department": (raw.get("Department") or {}).get("name", "") if isinstance(raw.get("Department"), dict) else (raw.get("Department") or ""),
            "employment_type": raw.get("Job_Type") or "",
            "raw_json": {"id": job_id, "company_name": ""},
        }
