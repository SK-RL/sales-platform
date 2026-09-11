"""Fetch open positions from Hireology career sites.

F390. Public JSON, no key:
``https://api.hireology.com/v2/public/careers/{slug}?page_size=100&page=N``
returns ``{"data": [...], "count", "page", "page_size"}`` (verified on
familiarroadshomehealthcareagency: 35 postings). Each posting carries
``id``, ``name``, ``locations[{city,state,zip_code}]``, ``remote``,
``employment_status``, ``career_site_path`` (``/{slug}/{id}/description``
on careers.hireology.com), ``job_family.name`` and ``compensation``.
The application form schema is public too (``/v2/public/
application_forms/{id}``) — see ``fetchers.questions``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

API_URL = "https://api.hireology.com/v2/public/careers/{slug}"
FORM_URL = "https://api.hireology.com/v2/public/application_forms/{job_id}"
SITE_URL = "https://careers.hireology.com/{slug}/{job_id}/description"
_PAGE_SIZE = 100
_MAX_PAGES = 10


class HireologyFetcher(BaseFetcher):
    PLATFORM = "hireology"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        jobs: list[dict] = []
        page = 1
        while page <= _MAX_PAGES:
            try:
                resp = client.get(API_URL.format(slug=slug), params={"page_size": _PAGE_SIZE, "page": page})
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.warning("Hireology %s returned %s", slug, exc.response.status_code)
                break
            except (httpx.RequestError, ValueError) as exc:
                logger.warning("Hireology %s request failed: %s", slug, exc)
                break
            items = data.get("data") or []
            jobs.extend(j for j in (self._normalize(i, slug) for i in items if isinstance(i, dict)) if j)
            total = int(data.get("count") or 0)
            if not items or page * _PAGE_SIZE >= total:
                break
            page += 1
        logger.info("Hireology %s fetched %d postings", slug, len(jobs))
        return jobs

    def fetch_one(self, slug: str, external_id: str) -> dict | None:
        ids = self._id_forms(external_id)
        for job in self.fetch(slug):
            if job["external_id"] in ids:
                return job
        job_id = next((i for i in ids if i.isdigit()), "")
        if not job_id:
            return None
        try:
            resp = self._get_client().get(FORM_URL.format(job_id=job_id))
            if resp.status_code != 200:
                return None
            form = resp.json()
        except (httpx.RequestError, ValueError):
            return None
        title = (form.get("form_title") or "").strip()
        title = title[len("Apply for "):] if title.lower().startswith("apply for ") else title
        path = (form.get("job") or {}).get("careers_pathname") or slug
        if not title:
            return None
        return self._normalize({"id": int(job_id), "name": title, "career_site_path": f"/{path}/{job_id}/description",
                                "status": "Open", "unlisted": True}, slug)

    def _normalize(self, raw: dict[str, Any], slug: str) -> dict | None:
        job_id = raw.get("id")
        title = (raw.get("name") or "").strip()
        if job_id is None or not title:
            return None
        if (raw.get("status") or "Open") != "Open":
            return None
        locs = raw.get("locations") or []
        parts = []
        for loc in locs:
            if isinstance(loc, dict):
                p = ", ".join(x for x in ((loc.get("city") or "").strip(), (loc.get("state") or "").strip()) if x)
                if p and p not in parts:
                    parts.append(p)
        location_raw = "; ".join(parts)
        remote = bool(raw.get("remote"))
        if remote:
            location_raw = "Remote" + (f" ({location_raw})" if location_raw else "")
        path = raw.get("career_site_path") or f"/{slug}/{job_id}/description"
        fam = raw.get("job_family") or {}
        comp = raw.get("compensation") or {}
        salary = ""
        if isinstance(comp, dict) and comp.get("comp_range_min") and comp.get("comp_range_max"):
            salary = f"{comp['comp_range_min']}-{comp['comp_range_max']} per {comp.get('comp_period') or 'year'}"
        return {
            "external_id": f"hireology-{job_id}",
            "company_slug": slug,
            "title": title,
            "url": "https://careers.hireology.com" + path,
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": "remote" if remote else (self._detect_remote_scope(location_raw, title) or ""),
            "department": (fam.get("name") if isinstance(fam, dict) else "") or "",
            "employment_type": raw.get("employment_status") or "",
            "posted_at": raw.get("created_at") or "",
            "salary_range": salary,
            "raw_json": {"id": job_id, "company_name": ((raw.get("organization") or {}).get("name") or ""),
                         "unlisted": bool(raw.get("unlisted")), "description": raw.get("job_description") or ""},
        }
