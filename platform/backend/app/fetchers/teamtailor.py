"""Fetch open positions from Teamtailor career sites.

F381. Public RSS feed, no key: ``https://{slug}.teamtailor.com/jobs.rss``
— one <item> per published job with title, link, description and
pubDate (verified on virtasant / clearroute / xci). The numeric id is
the first path segment after ``/jobs/`` in the link; the application
form lives at ``{link}/applications/new``. Some tenants use a custom
domain (careers.example.com) — those are reached by URL, not slug.
"""

from __future__ import annotations

import logging
import re
from html import unescape

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

FEED_URL = "https://{slug}.teamtailor.com/jobs.rss"
_ID = re.compile(r"/jobs/(\d+)")


def _tag(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
    return unescape(m.group(1)).strip() if m else ""


class TeamtailorFetcher(BaseFetcher):
    PLATFORM = "teamtailor"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            resp = client.get(FEED_URL.format(slug=slug))
        except httpx.RequestError as exc:
            logger.warning("Teamtailor %s request failed: %s", slug, exc)
            return []
        if resp.status_code != 200 or "<item>" not in resp.text:
            logger.warning("Teamtailor %s: no feed (HTTP %s)", slug, resp.status_code)
            return []
        items = re.findall(r"<item>(.*?)</item>", resp.text, re.S)
        out = [j for j in (self._normalize(i, slug) for i in items) if j]
        logger.info("Teamtailor %s fetched %d jobs", slug, len(out))
        return out

    def _normalize(self, block: str, slug: str) -> dict | None:
        link = _tag(block, "link")
        title = _tag(block, "title")
        m = _ID.search(link)
        if not m or not title:
            return None
        desc = re.sub(r"<[^>]+>", " ", unescape(_tag(block, "description")))
        desc = " ".join(desc.split())
        loc = _tag(block, "tt:location") or ", ".join(p for p in (_tag(block, "tt:city"), _tag(block, "tt:country")) if p)
        if not loc:
            lm = re.search(r"Location:\s*([^|.]{2,60})", desc)
            loc = lm.group(1).strip() if lm else ""
        status = _tag(block, "remoteStatus").lower()
        remote = status in ("fully_remote", "remote", "hybrid") or bool(re.search(r"\bremote\b", f"{loc} {title}", re.I))
        if status == "fully_remote" and not re.search(r"remote", loc, re.I):
            loc = f"Remote ({loc})" if loc else "Remote"
        return {
            "external_id": m.group(1),
            "company_slug": slug,
            "title": title,
            "url": link.split("?", 1)[0],
            "platform": self.PLATFORM,
            "location_raw": loc,
            "remote_scope": "remote" if remote else "",
            "department": _tag(block, "tt:department") or _tag(block, "department"),
            "posted_at": _tag(block, "pubDate") or None,
            "raw_json": {"id": m.group(1), "title": title, "link": link, "summary": desc[:400]},
        }
