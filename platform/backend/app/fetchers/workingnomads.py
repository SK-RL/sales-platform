"""F335 — Working Nomads JSON aggregator.

Working Nomads (https://www.workingnomads.com/) is a curated
remote-jobs aggregator. The 2026-05-09 audit recommended adding
it as a tier-1 win for the global_remote pool.

Endpoint
--------
Public JSON at https://www.workingnomads.com/api/exposed_jobs/
returns a flat array (~30-50 active jobs) with the following
shape per item:

    {
      "title":          "Director of Revenue Systems...",
      "company_name":   "Caul Group",
      "category_name":  "Finance",
      "location":       "LATAM",
      "tags":           "crm,operations,tech,...",
      "description":    "<p>...</p>",       # HTML
      "url":            "https://www.workingnomads.com/job/go/1588652/",
      "pub_date":       "2026-05-08T15:54:48-04:00"   # already ISO-8601
    }

Notes vs other RSS-style aggregators
------------------------------------
* Pre-F335-fixup: an earlier draft tried ``/jobsrss`` based on
  WN's public docs, but that endpoint now redirects to the SPA
  (verified 2026-05-09 — 301 → HTML). The JSON ``/api/
  exposed_jobs/`` endpoint is the live one and returns the same
  data set in a cleaner shape (no XML/CDATA parsing, pub_date
  is already ISO-8601 so the shared ``app/utils/rss.py``
  RFC-822 normaliser isn't needed here).
* Single board with slug='__all__' (matches WWR / RemoteOK /
  Himalayas / Remotive). The scanner's aggregator branch
  resolves per-job companies before upsert so each job lands
  under the real hirer's Company row instead of a synthetic
  "Working Nomads" parent.
"""

import logging
from typing import Any
from urllib.parse import urlparse

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

# Live JSON endpoint, verified 2026-05-09. Returns
# ``application/json`` body of length ~150-200KB with 30-50
# active job rows. No auth required.
API_URL = "https://www.workingnomads.com/api/exposed_jobs/"


class WorkingNomadsFetcher(BaseFetcher):
    """Fetch open positions from Working Nomads via the public JSON API."""

    PLATFORM = "workingnomads"

    def fetch(self, slug: str) -> list[dict]:
        """Fetch all WN jobs from the public JSON endpoint.

        ``slug`` is ignored; convention is ``"__all__"`` from the
        scanner so the per-board scan-log row carries a
        meaningful slug column.
        """
        client = self._get_client()
        all_jobs: list[dict] = []

        try:
            resp = client.get(
                API_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "JobPlatform/1.0 (job aggregator)",
                },
            )
            if resp.status_code != 200:
                logger.warning("Working Nomads API returned %s", resp.status_code)
                return []

            payload = resp.json()
            if not isinstance(payload, list):
                logger.warning(
                    "Working Nomads API returned unexpected shape: %s",
                    type(payload).__name__,
                )
                return []

            for item in payload:
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize(item)
                if normalized:
                    all_jobs.append(normalized)

        except Exception as exc:
            logger.warning("Working Nomads fetch failed: %s", exc)

        logger.info("Working Nomads fetched %d jobs from JSON API", len(all_jobs))
        return all_jobs

    def _normalize(self, raw: dict[str, Any]) -> dict:
        """Map a single API row to the canonical fetcher output
        shape. Returns ``{}`` (skip) for rows missing critical
        fields (title, url).
        """
        title = (raw.get("title") or "").strip()
        if not title:
            return {}

        job_url = (raw.get("url") or "").strip()
        if not job_url:
            return {}

        company_name = (raw.get("company_name") or "").strip()
        company_slug = (
            company_name.lower().replace(" ", "-").replace("/", "-")[:100]
            if company_name
            else "unknown"
        )

        description_html = (raw.get("description") or "").strip()
        location_raw = (raw.get("location") or "").strip()
        category = (raw.get("category_name") or "").strip()

        # ``pub_date`` is already ISO-8601 with timezone offset
        # (e.g. ``"2026-05-08T15:54:48-04:00"``). Pass through
        # unchanged — defensive ``.strip()`` only. Empty / null →
        # empty string, matching the upsert path's "give up
        # rather than guess" pattern for ambiguous timestamps.
        posted_at = (raw.get("pub_date") or "").strip()

        # Tags arrive as a comma-separated string. Split into a
        # list for downstream consumers (intelligence, role-
        # cluster auditing) but keep the raw form in raw_json.
        raw_tags = (raw.get("tags") or "").strip()
        tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]

        # External-id derivation: WN's job URL ends in
        # ``/job/go/<numeric-id>/`` — extract the numeric id for
        # idempotent re-scans (same job → same external_id).
        # Fallback to a hash of (company, title) if the URL shape
        # ever changes.
        ext_id_tail = ""
        try:
            path = urlparse(job_url).path.rstrip("/")
            ext_id_tail = path.rsplit("/", 1)[-1]
        except Exception:
            ext_id_tail = ""
        if not ext_id_tail or not ext_id_tail.isdigit():
            # Fallback: deterministic hash of the URL itself.
            # ``job_url`` has been validated non-empty above, so
            # this always produces a stable id.
            import hashlib
            ext_id_tail = hashlib.md5(job_url.encode("utf-8")).hexdigest()[:16]
        ext_id = f"workingnomads-{ext_id_tail}"

        # Detect remote scope using the inherited helper. WN is
        # by definition remote, but worldwide vs region-restricted
        # depends on the location string ("LATAM" / "Europe Only"
        # / "Worldwide"). Pass title too in case the location
        # column is empty.
        remote_scope = self._detect_remote_scope(location_raw, title) or "remote"

        return {
            "external_id": ext_id,
            "company_slug": company_slug,
            "company_name": company_name,
            "title": title,
            "url": job_url,
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": category,
            "employment_type": "",
            "salary_range": "",
            "posted_at": posted_at,
            "raw_json": {
                "title": title,
                "url": job_url,
                "company_name": company_name,
                "category_name": category,
                "location": location_raw,
                "tags": raw_tags,
                "tag_list": tag_list,
                "pub_date": posted_at,
                "description": description_html,
            },
        }
