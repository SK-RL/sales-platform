"""Fetch open positions from Breezy HR.

F377. Public JSON feed, no key: ``https://{slug}.breezy.hr/json`` returns
every published position (verified live on vetsez — 62 positions;
a missing board is a 404 → empty list). Each item carries the posting
URL (``/p/{id}-{slug}``), a structured location with ``is_remote`` and
``remote_details``, the department and the publish date.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

API_URL = "https://{slug}.breezy.hr/json"


class BreezyFetcher(BaseFetcher):
    PLATFORM = "breezy"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            resp = client.get(API_URL.format(slug=slug))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Breezy %s returned %s", slug, exc.response.status_code)
            return []
        except httpx.RequestError as exc:
            logger.warning("Breezy %s request failed: %s", slug, exc)
            return []
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("Breezy %s returned non-JSON: %s", slug, exc)
            return []
        if not isinstance(data, list):
            logger.warning("Breezy %s unexpected payload type: %s", slug, type(data).__name__)
            return []
        logger.info("Breezy %s fetched %d positions", slug, len(data))
        return [self._normalize(item, slug) for item in data if isinstance(item, dict)]

    def _normalize(self, raw: dict[str, Any], slug: str) -> dict:
        loc = raw.get("location") or {}
        is_remote = bool(loc.get("is_remote"))
        remote_details = (loc.get("remote_details") or {}).get("value") or ""
        location_raw = loc.get("name") or ", ".join(
            p for p in ((loc.get("city") or ""), ((loc.get("country") or {}).get("name") or "")) if p
        )
        if is_remote:
            location_raw = f"Remote ({location_raw})" if location_raw else "Remote"
        if remote_details == "remote-anywhere":
            remote_scope = "global"
        elif is_remote:
            remote_scope = "remote"
        else:
            remote_scope = ""
        dept = raw.get("department")
        department = (dept.get("name") if isinstance(dept, dict) else dept) or ""
        company = raw.get("company") or {}
        return {
            "external_id": str(raw.get("id", "")),
            "company_slug": slug,
            "title": (raw.get("name") or "").strip(),
            "url": raw.get("url") or f"https://{slug}.breezy.hr/p/{raw.get('friendly_id') or raw.get('id')}",
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": department,
            "employment_type": ((raw.get("type") or {}).get("name") or "") if isinstance(raw.get("type"), dict) else "",
            "posted_at": raw.get("published_date"),
            "raw_json": {**raw, "company_name": company.get("name") or ""},
        }
