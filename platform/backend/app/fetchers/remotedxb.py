"""RemoteDXB fetcher — curated UAE remote/hybrid board with a JSON API.

RemoteDXB (https://www.remotedxb.com) is a UAE-focused remote + hybrid
jobs board. Unlike the noisy meta-aggregators, it exposes a clean,
paginated JSON API at ``/api/v1/listings`` (Laravel-style: ``?page=N``,
``meta.next_page_url`` / ``meta.last_page``) with REAL employer names
(NMC Healthcare, Chalhoub Group, Honeywell, Oracle …), an explicit
``remote_status_label`` ("Completely Remote" / "Hybrid" / "Hybrid:
Dubai" / "Hybrid: Abu Dhabi"), and ``primary_country_code`` — exactly
the UAE hybrid signal the ATS/aggregator sources were missing.

robots.txt allows ``/api/`` (only /admin,/user,/billing,/stripe are
disallowed) and explicitly allows ClaudeBot.

Classification mapping:
  * ``remote_status_label`` → ``remote_scope`` ("hybrid"/"remote").
  * location = ``location.name`` → else the city in the label
    ("Hybrid: Dubai" → "Dubai") → else the country from
    ``primary_country_code`` (AE → "United Arab Emirates").
  So "Hybrid: Dubai" resolves to ``("hybrid", ["AE"])`` and "Completely
  Remote" (no country) to ``("worldwide", [])`` via the shared
  ``classify_remote_policy`` heuristics.

Slug convention: ignored (single board); use ``__all__`` like the other
aggregator boards.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

API_URL = "https://www.remotedxb.com/api/v1/listings"

# 10 jobs/page × 100 = up to 1000 jobs/scan. The board is ordered
# newest-first, so the freshest postings are always covered; the
# upsert-on-external-id semantics let later scans reach deeper.
_MAX_PAGES = 100

# Two-letter country code → a location string the classifier recognises.
_CC_TO_LOCATION = {"AE": "United Arab Emirates"}


class RemoteDXBFetcher(BaseFetcher):
    """Fetcher for the RemoteDXB ``/api/v1/listings`` JSON API."""

    PLATFORM = "remotedxb"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        all_jobs: list[dict] = []
        seen: set[str] = set()
        url: Optional[str] = API_URL

        for _page in range(1, _MAX_PAGES + 1):
            if not url:
                break
            try:
                resp = client.get(url, headers={"Accept": "application/json"})
            except httpx.RequestError as exc:
                logger.warning("RemoteDXB request failed at %s: %s", url, exc)
                break
            if not resp.is_success:
                logger.warning("RemoteDXB %s -> %s", url, resp.status_code)
                break

            try:
                data = resp.json()
            except ValueError:
                logger.warning("RemoteDXB returned non-JSON at %s", url)
                break

            items = data.get("data") or []
            if not items:
                break

            new_on_page = 0
            for raw in items:
                job = self._normalize(raw, slug)
                if job and job["external_id"] not in seen:
                    seen.add(job["external_id"])
                    all_jobs.append(job)
                    new_on_page += 1

            url = (data.get("meta") or {}).get("next_page_url")
            if new_on_page == 0:
                break

        logger.info("RemoteDXB fetched %d jobs", len(all_jobs))
        return all_jobs

    def _normalize(self, raw: dict[str, Any], slug: str) -> Optional[dict]:
        link = (raw.get("link") or "").strip()
        title = (raw.get("title") or "").strip()
        if not link or not title:
            return None

        # external_id — the trailing ``--<id>`` on the job link is stable;
        # fall back to the whole link.
        m = re.search(r"--(\d+)/?$", link)
        ext = m.group(1) if m else link

        company = (raw.get("company_name") or "").strip()

        label = (raw.get("remote_status_label") or "").strip()
        low = label.lower()
        if "hybrid" in low:
            remote_scope = "hybrid"
        elif "remote" in low:
            remote_scope = "remote"
        elif "on-site" in low or "onsite" in low or "on site" in low:
            remote_scope = "onsite"
        else:
            remote_scope = ""

        loc = raw.get("location")
        location_raw = (loc.get("name") or "").strip() if isinstance(loc, dict) else ""
        if not location_raw and ":" in label:
            # "Hybrid: Dubai" → "Dubai"
            location_raw = label.split(":", 1)[1].strip()
        if not location_raw:
            cc = (raw.get("primary_country_code") or "").strip().upper()
            location_raw = _CC_TO_LOCATION.get(cc, cc)

        commitment = raw.get("commitment")
        emp = commitment.get("name", "") if isinstance(commitment, dict) else ""
        category = raw.get("category")
        dept = category.get("name", "") if isinstance(category, dict) else ""
        salary = raw.get("salary_range")
        salary = salary.strip() if isinstance(salary, str) else ""

        return {
            "external_id": f"remotedxb-{ext}",
            "company_slug": self._slugify(company) or "remotedxb-unknown",
            "company_name": company,
            "title": title,
            "url": link,
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": dept,
            "employment_type": emp,
            "salary_range": salary,
            "posted_at": raw.get("published_at"),
            "raw_json": {
                "remote_status_label": label,
                "primary_country_code": raw.get("primary_country_code"),
                "location": location_raw,
            },
        }

    @staticmethod
    def _slugify(name: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
        return s[:200]
