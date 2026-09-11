"""Fetch open positions from Rippling ATS.

F379. Public JSON API, no key:
``https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs`` — a
list of ``{uuid, name, department, url, workLocation}`` (verified live on
athennian; an unknown board is a 404 → empty). The posting URL is
``https://ats.rippling.com/{slug}/jobs/{uuid}`` and the form
``…/apply``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

API_URL = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"


class RipplingFetcher(BaseFetcher):
    PLATFORM = "rippling"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            resp = client.get(API_URL.format(slug=slug))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Rippling %s returned %s", slug, exc.response.status_code)
            return []
        except httpx.RequestError as exc:
            logger.warning("Rippling %s request failed: %s", slug, exc)
            return []
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("Rippling %s returned non-JSON: %s", slug, exc)
            return []
        items = data if isinstance(data, list) else (data.get("items") or data.get("jobs") or []) if isinstance(data, dict) else []
        # One posting per uuid: the API repeats a job once per work
        # location, and the board's posting URL is the same for all.
        out: dict[str, dict] = {}
        for i in items:
            j = self._normalize(i, slug) if isinstance(i, dict) else None
            if not j:
                continue
            if j["external_id"] in out:
                prev = out[j["external_id"]]
                if j["location_raw"] and j["location_raw"] not in prev["location_raw"]:
                    prev["location_raw"] = f"{prev['location_raw']}; {j['location_raw']}".strip("; ")
                if j["remote_scope"]:
                    prev["remote_scope"] = j["remote_scope"]
                continue
            out[j["external_id"]] = j
        logger.info("Rippling %s fetched %d jobs (%d postings)", slug, len(items), len(out))
        return list(out.values())

    def _normalize(self, raw: dict[str, Any], slug: str) -> dict | None:
        uuid_ = str(raw.get("uuid") or raw.get("id") or "")
        title = (raw.get("name") or raw.get("title") or "").strip()
        if not uuid_ or not title:
            return None
        loc = raw.get("workLocation") or {}
        if isinstance(loc, dict):
            location_raw = loc.get("label") or loc.get("name") or ", ".join(
                p for p in (loc.get("city") or "", loc.get("state") or "", loc.get("country") or "") if p)
            remote = bool(loc.get("isRemote") or loc.get("remote")) or "remote" in str(loc).lower()
        else:
            location_raw = str(loc or "")
            remote = "remote" in location_raw.lower()
        dept = raw.get("department")
        return {
            "external_id": uuid_,
            "company_slug": slug,
            "title": title,
            "url": raw.get("url") or f"https://ats.rippling.com/{slug}/jobs/{uuid_}",
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": "remote" if remote else "",
            "department": (dept.get("name") if isinstance(dept, dict) else dept) or "",
            "raw_json": raw,
        }
