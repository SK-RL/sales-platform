"""Fetch open positions from Pinpoint (pinpointhq.com career sites).

F387. Public JSON feed, no key: ``https://{slug}.pinpointhq.com/postings.json``
returns ``{"data": [...]}`` — verified live on made-tech (23), coforma,
wealthwizards. Each posting carries an ``id``, an ``/en/postings/{uuid}``
url, nested ``location`` and ``job`` (department), a compensation string
and ``workplace_type_text``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

FEED_URL = "https://{slug}.pinpointhq.com/postings.json"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _description_html(raw: dict) -> str:
    """F405 — Pinpoint splits the posting into description /
    responsibilities / skills / benefits, each HTML with its own header."""
    parts = []
    for key in ("description", "key_responsibilities", "skills_knowledge_expertise", "benefits"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            header = raw.get(f"{key}_header")
            parts.append((f"<h3>{header}</h3>" if isinstance(header, str) and header.strip() else "") + v)
    return "".join(parts)


class PinpointFetcher(BaseFetcher):
    PLATFORM = "pinpoint"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            resp = client.get(FEED_URL.format(slug=slug))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Pinpoint %s returned %s", slug, exc.response.status_code)
            return []
        except httpx.RequestError as exc:
            logger.warning("Pinpoint %s request failed: %s", slug, exc)
            return []
        try:
            data = resp.json().get("data", [])
        except Exception as exc:
            logger.warning("Pinpoint %s non-JSON: %s", slug, exc)
            return []
        logger.info("Pinpoint %s fetched %d postings", slug, len(data))
        return [j for j in (self._normalize(i, slug) for i in data if isinstance(i, dict)) if j]

    def _normalize(self, raw: dict[str, Any], slug: str) -> dict | None:
        title = (raw.get("title") or "").strip()
        url = raw.get("url") or ""
        # The feed's ``id`` is numeric but the public URL (and so a pasted
        # link) carries a UUID; key on the UUID so both paths agree.
        m = _UUID_RE.search(url or raw.get("path") or "")
        pid = m.group(0).lower() if m else str(raw.get("id") or "")
        if not pid or not title or not url:
            return None
        loc = raw.get("location") or {}
        location_raw = loc.get("name") or ", ".join(p for p in (loc.get("city") or "", loc.get("province") or "") if p)
        workplace = (raw.get("workplace_type_text") or raw.get("workplace_type") or "").lower()
        remote = "remote" in f"{workplace} {location_raw}".lower()
        job = raw.get("job") or {}
        dept = (job.get("department") or {})
        return {
            "external_id": f"pinpoint-{pid}",  # jobs.external_id is UNIQUE across platforms
            "company_slug": slug,
            "title": title,
            "url": url,
            "platform": self.PLATFORM,
            "location_raw": ("Remote" if remote and not loc.get("city") else location_raw) or "",
            "remote_scope": "remote" if remote else "",
            "department": (dept.get("name") if isinstance(dept, dict) else dept) or "",
            "employment_type": raw.get("employment_type_text") or raw.get("employment_type") or "",
            "salary_range": raw.get("compensation") if raw.get("compensation_visible") in (True, "True", "true") else "",
            "raw_json": {"id": pid, "feed_id": str(raw.get("id") or ""), "company_name": "", "path": raw.get("path"),
                         "description": _description_html(raw)},
        }
