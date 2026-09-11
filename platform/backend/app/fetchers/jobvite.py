"""Fetch open positions from Jobvite career sites.

HISTORY — 2026-04-17 the JSON API at ``jobs.jobvite.com/{slug}/jobs``
(``?availableTo=External&page=N``) was found retired: 14 historical
customers all 302'd to the Jobvite support page. F388 (2026-09-11)
re-surveyed: the host is alive for current tenants (``progress``:
31 postings) but serves **server-rendered HTML**, not JSON —
``table.jv-job-list`` rows with ``td.jv-job-list-name a[href=/{slug}/
job/{id}]`` and ``td.jv-job-list-location``, grouped under an
``<h3 class="h2">`` department heading. Unknown slugs still 302 to
``search.jobvite.com/?invalid=1`` and are treated as dead. There is no
pagination (``?page=2`` returns the same page).

The detail page ``/{slug}/job/{id}`` carries the title in ``<title>``
("{Company} Careers - {title}") and department + location in
``p.jv-job-detail-meta``; ``fetch_one`` reads it so a pasted link
resolves even when the board page is unavailable.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

API_URL = "https://jobs.jobvite.com/{slug}/jobs"
BOARD_URL = API_URL
DETAIL_URL = "https://jobs.jobvite.com/{slug}/job/{job_id}"

_DEAD_HOSTS = ("www.jobvite.com", "search.jobvite.com")
_SECTION_RE = re.compile(r'<h3 class="h2">(.*?)</h3>|<td class="jv-job-list-name">\s*<a href="/([^/"]+)/job/([A-Za-z0-9]+)"[^>]*>(.*?)</a>\s*</td>\s*<td class="jv-job-list-location">(.*?)</td>', re.S | re.I)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_META_RE = re.compile(r'<p class="jv-job-detail-meta">(.*?)</p>', re.S | re.I)


def _description_html(page: str) -> str:
    """F405 — the posting body on a Jobvite detail page."""
    try:
        from bs4 import BeautifulSoup

        node = BeautifulSoup(page or "", "html.parser").find(class_="jv-job-detail-description")
        return node.decode_contents().strip() if node else ""
    except Exception:
        return ""
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def _location(fragment: str) -> str:
    return re.sub(r"\s*,\s*", ", ", _text(fragment)).strip(", ")


class JobviteFetcher(BaseFetcher):
    """Fetch open positions from Jobvite."""

    PLATFORM = "jobvite"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            resp = client.get(BOARD_URL.format(slug=slug), params={"availableTo": "External"})
        except httpx.RequestError as exc:
            logger.warning("Jobvite %s request failed: %s", slug, exc)
            return []
        if self._dead(resp):
            logger.info("Jobvite %s: slug no longer hosted on jobs.jobvite.com", slug)
            return []
        if resp.status_code != 200:
            logger.warning("Jobvite %s returned %s", slug, resp.status_code)
            return []
        jobs = self.parse_board(resp.text, slug)
        logger.info("Jobvite %s fetched %d postings", slug, len(jobs))
        return jobs

    @staticmethod
    def _dead(resp: httpx.Response) -> bool:
        host = str(resp.url.host or "") if resp.url else ""
        return any(host.endswith(h) for h in _DEAD_HOSTS)

    def parse_board(self, page: str, slug: str) -> list[dict]:
        jobs: list[dict] = []
        seen: set[str] = set()
        department = ""
        for m in _SECTION_RE.finditer(page or ""):
            if m.group(1) is not None:
                department = _text(m.group(1))
                continue
            job_id, title, loc = m.group(3), _text(m.group(4)), _location(m.group(5))
            if not job_id or not title or job_id in seen:
                continue
            seen.add(job_id)
            jobs.append(self._normalize({"eId": job_id, "title": title, "location": loc, "category": department}, slug))
        return jobs

    def fetch_one(self, slug: str, external_id: str) -> dict | None:
        ids = self._id_forms(external_id)
        job_id = next((i for i in ids if not i.startswith("jobvite-")), "")
        listed = next((j for j in self.fetch(slug) if j["external_id"] in ids or j["raw_json"].get("eId") in ids), None)
        if not job_id:
            return listed
        try:
            resp = self._get_client().get(DETAIL_URL.format(slug=slug, job_id=job_id))
        except httpx.RequestError:
            return listed
        if resp.status_code != 200 or self._dead(resp) or "error=404" in str(resp.url):
            return listed
        if listed is not None:
            # F405 — the board list has no posting body; the detail page does.
            desc = _description_html(resp.text)
            if desc:
                listed["raw_json"]["description"] = desc
            return listed
        return self.parse_detail(resp.text, slug, job_id)

    def parse_detail(self, page: str, slug: str, job_id: str) -> dict | None:
        tm = _TITLE_RE.search(page or "")
        title = _text(tm.group(1)) if tm else ""
        title = re.sub(r"^.*?\bcareers\s*-\s*", "", title, flags=re.I).strip() or title
        if not title:
            return None
        department, location = "", ""
        mm = _META_RE.search(page or "")
        if mm:
            parts = [p for p in (_text(p) for p in re.split(r"<span class='jv-inline-separator'></span>|<span class=\"jv-inline-separator\"></span>", mm.group(1))) if p]
            if len(parts) >= 2:
                department, location = parts[0], _location(", ".join(parts[1:]))
            elif parts:
                location = _location(parts[0])
        job = self._normalize({"eId": job_id, "title": title, "location": location, "category": department, "unlisted": True}, slug)
        if job:
            desc = _description_html(page)
            if desc:
                job["raw_json"]["description"] = desc  # F405
        return job

    def _normalize(self, raw: dict[str, Any], slug: str) -> dict:
        job_id = raw.get("eId", "") or raw.get("id", "")
        title = raw.get("title", "")

        location_raw = raw.get("location", "") or ""
        if isinstance(location_raw, dict):
            parts = [location_raw.get("city", ""), location_raw.get("state", ""), location_raw.get("country", "")]
            location_raw = ", ".join(p for p in parts if p)

        department = raw.get("category", "") or raw.get("department", "") or ""
        job_type = raw.get("type", "") or ""

        job_url = raw.get("detailUrl", "") or raw.get("applyUrl", "")
        if not job_url and job_id:
            job_url = DETAIL_URL.format(slug=slug, job_id=job_id)

        posted_at = raw.get("postingDate", "") or raw.get("datePosted", "") or ""

        remote_scope = self._detect_remote_scope(location_raw, title)

        return {
            "external_id": f"jobvite-{job_id}" if job_id else f"jobvite-{slug}-{title[:50]}",
            "company_slug": slug,
            "title": title,
            "url": job_url,
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": department,
            "employment_type": job_type,
            "posted_at": posted_at,
            "raw_json": raw,
        }
