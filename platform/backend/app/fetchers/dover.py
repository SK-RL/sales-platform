"""Fetch open positions from Dover careers pages (app.dover.com).

F389. Public JSON, no key. Two calls: ``/api/v1/careers-page-slug/{slug}``
resolves the tenant id, then ``/api/v1/job-groups/{id}/job-groups``
lists groups of published jobs (``id`` uuid, ``title``, ``locations``,
``workplace_type``). A posting's own record — including its
``application_questions`` — is ``/api/v1/inbound/application-portal-job/
{uuid}``. Verified on dover, trycoast, mandrel, lexoga.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

SLUG_URL = "https://app.dover.com/api/v1/careers-page-slug/{slug}"
GROUPS_URL = "https://app.dover.com/api/v1/job-groups/{client_id}/job-groups"
JOB_URL = "https://app.dover.com/api/v1/inbound/application-portal-job/{job_id}"
APPLY_URL = "https://app.dover.com/apply/{slug}/{job_id}"


class DoverFetcher(BaseFetcher):
    PLATFORM = "dover"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            resp = client.get(SLUG_URL.format(slug=slug))
            if resp.status_code != 200:
                logger.info("Dover %s: no careers page (%s)", slug, resp.status_code)
                return []
            client_id = resp.json().get("id")
            if not client_id:
                return []
            resp = client.get(GROUPS_URL.format(client_id=client_id))
            resp.raise_for_status()
            groups = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("Dover %s returned %s", slug, exc.response.status_code)
            return []
        except (httpx.RequestError, ValueError) as exc:
            logger.warning("Dover %s request failed: %s", slug, exc)
            return []
        jobs: list[dict] = []
        for grp in groups if isinstance(groups, list) else []:
            for raw in grp.get("jobs") or []:
                j = self._normalize(raw, slug, group=grp.get("name") or "")
                if j:
                    jobs.append(j)
        logger.info("Dover %s fetched %d postings", slug, len(jobs))
        return jobs

    def fetch_one(self, slug: str, external_id: str) -> dict | None:
        ids = self._id_forms(external_id)
        job_id = next((i for i in ids if "-" in i and not i.startswith("dover-")), "")
        if not job_id:
            return None
        try:
            resp = self._get_client().get(JOB_URL.format(job_id=job_id))
            if resp.status_code != 200:
                return None
            raw = resp.json()
        except (httpx.RequestError, ValueError):
            return None
        if not raw.get("active", True):
            return None
        return self._normalize(raw, slug)

    def _normalize(self, raw: dict[str, Any], slug: str, group: str = "") -> dict | None:
        job_id = raw.get("id")
        title = (raw.get("title") or "").strip()
        if not job_id or not title or raw.get("is_published") is False or raw.get("is_sample"):
            return None
        locs = raw.get("locations") or []
        names = []
        for loc in locs:
            if isinstance(loc, dict):
                n = (loc.get("name") or (loc.get("location_option") or {}).get("display_name") or "").strip()
                if n and n not in names:
                    names.append(n)
        workplace = (raw.get("workplace_type") or "").upper()
        remote = workplace == "REMOTE" or any((l.get("location_type") or "").upper() == "REMOTE" for l in locs if isinstance(l, dict))
        location_raw = "; ".join(names)
        if remote:
            location_raw = "Remote" + (f" ({location_raw})" if location_raw else "")
        comp = raw.get("compensation") or {}
        salary = ""
        if isinstance(comp, dict) and comp.get("lower_bound") and comp.get("upper_bound"):
            salary = f"{comp.get('currency_code') or ''} {comp['lower_bound']}-{comp['upper_bound']}".strip()
        return {
            "external_id": f"dover-{job_id}",
            "company_slug": slug,
            "title": title,
            "url": APPLY_URL.format(slug=slug, job_id=job_id),
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": "remote" if remote else "",
            "department": group if group and group.lower() != "ungrouped" else "",
            "employment_type": (comp.get("employment_type") if isinstance(comp, dict) else "") or "",
            "posted_at": raw.get("created") or "",
            "salary_range": salary,
            "raw_json": {"id": job_id, "company_name": raw.get("client_name") or "",
                         "application_questions": raw.get("application_questions"),
                         "visa_support": raw.get("visa_support")},
        }
