"""Fetch open positions from Himalayas.app public API."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

# Himalayas API docs: https://himalayas.app/jobs/api
# For company-specific jobs: /api/v1/jobs?company_slug={slug}
# Returns: { jobs: [...], pagination: {...} }
API_URL = "https://himalayas.app/jobs/api?limit=100&offset={offset}"
COMPANY_API_URL = "https://himalayas.app/jobs/api?company_slug={slug}&limit=100&offset={offset}"


class HimalayasFetcher(BaseFetcher):
    """Fetch open positions from Himalayas.app."""

    PLATFORM = "himalayas"

    # Safety ceiling. Per-company calls pass a slug so the catalog is small;
    # the aggregator __all__ path pulls the whole board, which recently
    # exceeded the old 1020 cap and got the scan stuck on the first 1020
    # rows every run (regression finding 17). Raising to 20k covers the
    # whole current catalog with headroom and still bounds the worst case
    # if the API misreports `totalCount`.
    _MAX_JOBS_PER_SCAN = 20000

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        all_jobs = []
        offset = 0

        while True:
            if slug == "__all__":
                url = API_URL.format(offset=offset)
            else:
                url = COMPANY_API_URL.format(slug=slug, offset=offset)

            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning("Himalayas %s returned %s", slug, exc.response.status_code)
                break
            except httpx.RequestError as exc:
                logger.warning("Himalayas %s request failed: %s", slug, exc)
                break

            data = resp.json()
            jobs = data.get("jobs", [])
            if not jobs:
                break

            all_jobs.extend([self._normalize(job, slug) for job in jobs])

            # API returns totalCount at top level or in pagination
            pagination = data.get("pagination", {})
            total = data.get("totalCount") or pagination.get("totalCount", 0)
            if offset + len(jobs) >= total:
                break
            offset += len(jobs)

            # Safety ceiling — bounds runaway pagination if the API lies
            # about totalCount. See _MAX_JOBS_PER_SCAN note above.
            if offset >= self._MAX_JOBS_PER_SCAN:
                logger.warning(
                    "Himalayas %s hit pagination safety ceiling at offset=%s",
                    slug, offset,
                )
                break

        return all_jobs

    def _normalize(self, raw: dict[str, Any], slug: str) -> dict:
        title = raw.get("title", "")
        company_name = raw.get("companyName", "") or ""
        company_slug = raw.get("companySlug", slug) or slug

        # Location
        location_parts = []
        if raw.get("location"):
            location_parts.append(raw["location"])
        elif raw.get("locationRestrictions"):
            restrictions = raw["locationRestrictions"]
            if isinstance(restrictions, list):
                location_parts.extend(str(r) for r in restrictions[:3])

        location_raw = ", ".join(location_parts) if location_parts else ""

        # URL — API changed: now uses applicationLink / guid
        job_url = (
            raw.get("applicationLink", "")
            or raw.get("guid", "")
            or raw.get("applicationUrl", "")
            or raw.get("url", "")
        )

        # Generate a unique external ID from guid or title+company
        guid = raw.get("guid", "")
        if guid:
            # Extract the slug from the guid URL
            ext_id = guid.rstrip("/").split("/")[-1]
        else:
            ext_id = f"{company_slug}-{title}".lower().replace(" ", "-")[:100]

        # Department / category — use parentCategories first
        parent_cats = raw.get("parentCategories", [])
        categories = raw.get("categories", [])
        department = ""
        if isinstance(parent_cats, list) and parent_cats:
            department = parent_cats[0]
        elif isinstance(categories, list) and categories:
            department = categories[0]

        # Employment type
        employment_type = raw.get("employmentType", "") or raw.get("type", "") or ""

        # Salary
        salary_parts = []
        currency = raw.get("currency", "")
        min_sal = raw.get("minSalary")
        max_sal = raw.get("maxSalary")
        if currency and (min_sal or max_sal):
            if min_sal and max_sal:
                salary_parts.append(f"{currency} {min_sal} - {max_sal}")
            elif min_sal:
                salary_parts.append(f"{currency} {min_sal}+")
            elif max_sal:
                salary_parts.append(f"Up to {currency} {max_sal}")
        salary_range = " ".join(salary_parts) if salary_parts else ""

        # Remote scope — Himalayas jobs are remote-first
        remote_scope = self._detect_remote_scope(location_raw)
        if not remote_scope:
            remote_scope = "remote"

        # Posted date — pubDate is now a unix timestamp
        posted_at_raw = raw.get("pubDate", "") or raw.get("publishedAt", "")
        posted_at = ""
        if isinstance(posted_at_raw, (int, float)) and posted_at_raw > 1000000000:
            from datetime import datetime as dt, timezone as tz
            posted_at = dt.fromtimestamp(posted_at_raw, tz=tz.utc).isoformat()
        elif isinstance(posted_at_raw, str) and posted_at_raw:
            posted_at = posted_at_raw

        return {
            "external_id": f"himalayas-{ext_id}",
            "company_slug": company_slug,
            # F340 regression fix: pre-fix the Himalayas fetcher
            # omitted ``company_name`` from its return dict. The
            # downstream aggregator-resolution path in
            # ``scan_task.py`` had to fall back to
            # ``raw_json.companyName``, which sometimes carries a
            # stale / wrong value while ``companySlug`` (used by
            # the public URL) is correct. The scanner then derived
            # the lookup slug from the wrong name and bound the job
            # to a different Company row (user feedback 2026-04-29:
            # "showing about 'Senior Cloud Engineer' job at 'Ähdus
            # Technology' but it is showing the job details of a
            # different company"). Surfacing ``company_name``
            # explicitly here lets the scanner ALSO use it for
            # display while preferring ``companySlug`` for binding.
            "company_name": company_name,
            "title": title,
            "url": job_url,
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": department,
            "employment_type": employment_type,
            "salary_range": salary_range,
            "posted_at": posted_at,
            "raw_json": raw,
        }
