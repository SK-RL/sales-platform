"""Fetch open positions from JazzHR (applytojob.com career pages).

F380. JazzHR has no public feed (the RSS and feed routes are gone —
410/404, verified); the board page ``https://{slug}.applytojob.com/apply/``
is server-rendered with one ``li.list-group-item`` per posting: an
``<a href=".../apply/{code}/{Title-Slug}">`` heading and an inline list
whose first entry is the location. The posting code is the external id.
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

BOARD_URL = "https://{slug}.applytojob.com/apply/"
_LINK = re.compile(r"^https?://[a-z0-9-]+\.applytojob\.com/apply/([A-Za-z0-9]+)/?", re.I)


class JazzHRFetcher(BaseFetcher):
    PLATFORM = "jazzhr"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            resp = client.get(BOARD_URL.format(slug=slug))
        except httpx.RequestError as exc:
            logger.warning("JazzHR %s request failed: %s", slug, exc)
            return []
        # An unknown slug redirects to the JazzHR marketing site.
        if resp.status_code != 200 or "applytojob.com" not in str(resp.url):
            logger.warning("JazzHR %s: no board (HTTP %s at %s)", slug, resp.status_code, resp.url)
            return []
        return self.parse_board(resp.text, slug)

    def parse_board(self, html: str, slug: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        out: list[dict] = []
        seen: set[str] = set()
        for item in soup.select("li.list-group-item"):
            a = item.select_one("h3 a[href], a[href]")
            if not a:
                continue
            m = _LINK.match(a.get("href", ""))
            if not m:
                continue
            code = m.group(1)
            if code in seen:
                continue
            seen.add(code)
            title = " ".join(a.get_text(" ", strip=True).split())
            meta = [" ".join(li.get_text(" ", strip=True).split()) for li in item.select("ul.list-inline li")]
            location = meta[0] if meta else ""
            out.append({
                "external_id": code,
                "company_slug": slug,
                "title": title,
                "url": a["href"].split("?", 1)[0],
                "platform": self.PLATFORM,
                "location_raw": location,
                "remote_scope": "remote" if re.search(r"\bremote\b", location, re.I) else "",
                "department": meta[1] if len(meta) > 1 else "",
                "raw_json": {"code": code, "meta": meta},
            })
        logger.info("JazzHR %s fetched %d postings", slug, len(out))
        return out

    def fetch_one(self, slug: str, external_id: str) -> dict | None:
        """A posting can be live while unlisted on the board (seen on
        lumivero: the DevOps page answers 200 but the board omits it), so
        a pasted link is resolved from the posting page itself."""
        listed = next((j for j in self.fetch(slug) if j["external_id"] == external_id), None)
        if listed:
            return listed
        client = self._get_client()
        try:
            resp = client.get(f"https://{slug}.applytojob.com/apply/{external_id}/")
        except httpx.RequestError:
            return None
        if resp.status_code != 200 or "applytojob.com" not in str(resp.url):
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        title = " ".join((soup.title.get_text() if soup.title else "").split())
        title = re.sub(r"\s*-\s*Career Page\s*$", "", title).strip()
        if not title:
            return None
        return {
            "external_id": external_id, "company_slug": slug, "title": title,
            "url": f"https://{slug}.applytojob.com/apply/{external_id}/", "platform": self.PLATFORM,
            "location_raw": "", "remote_scope": "", "department": "", "raw_json": {"code": external_id, "unlisted": True},
        }
