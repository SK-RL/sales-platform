"""Fetch open positions from Personio.

F378. Public XML feed, no key: ``https://{slug}.jobs.personio.com/xml``
(``.de`` for tenants on the German domain — tried second). Each
``<position>`` carries id, name, office, department, employmentType,
schedule, seniority, createdAt and HTML descriptions, but no URL: the
posting lives at ``https://{slug}.jobs.personio.com/job/{id}`` and the
form at ``…/job/{id}/apply``. Verified live on greenbone-ag.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

HOSTS = ("https://{slug}.jobs.personio.com", "https://{slug}.jobs.personio.de")


def _text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
    return (m.group(1) if m else "").strip()


class PersonioFetcher(BaseFetcher):
    PLATFORM = "personio"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        for host in HOSTS:
            base = host.format(slug=slug)
            try:
                resp = client.get(f"{base}/xml")
            except httpx.RequestError as exc:
                logger.warning("Personio %s request failed: %s", base, exc)
                continue
            if resp.status_code != 200 or "<position>" not in resp.text:
                continue
            blocks = re.findall(r"<position>(.*?)</position>", resp.text, re.S)
            logger.info("Personio %s fetched %d positions", slug, len(blocks))
            return [j for j in (self._normalize(b, slug, base) for b in blocks) if j]
        logger.warning("Personio %s: no feed on either host", slug)
        return []

    def _normalize(self, block: str, slug: str, base: str) -> dict | None:
        pid = _text(block, "id")
        title = _text(block, "name")
        if not pid or not title:
            return None
        office = _text(block, "office")
        schedule = _text(block, "schedule")
        remote = bool(re.search(r"remote|home ?office", f"{office} {title}", re.I))
        return {
            "external_id": pid,
            "company_slug": slug,
            "title": title,
            "url": f"{base}/job/{pid}",
            "platform": self.PLATFORM,
            "location_raw": ("Remote" if remote and not office else office),
            "remote_scope": "remote" if remote else "",
            "department": _text(block, "department"),
            "employment_type": schedule or _text(block, "employmentType"),
            "posted_at": _text(block, "createdAt") or None,
            "raw_json": {
                "id": pid, "name": title, "office": office, "department": _text(block, "department"),
                "seniority": _text(block, "seniority"), "schedule": schedule,
                "company_name": _text(block, "subcompany"), "base": base,
            },
        }
