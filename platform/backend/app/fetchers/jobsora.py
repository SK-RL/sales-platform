"""Jobsora fetcher — country-scoped job aggregator (HTML, no API).

Jobsora (https://jobsora.com) runs per-country subdomains
(``ae.jobsora.com`` for the UAE) whose category listing pages are
**server-rendered** — each job is an ``<article class="…
js-vacancy-snippet">`` card we can parse without a headless browser.

robots.txt
----------
Jobsora's robots.txt is ``Disallow: /*?`` — i.e. any URL with a query
string is off-limits to crawlers. We ONLY ever request PATH-based
category pages, which are allowed:

    https://ae.jobsora.com/jobs-remote          (page 1)
    https://ae.jobsora.com/jobs-remote-2        (page 2)
    ...

Pagination is the ``-N`` suffix on the path (also robots-clean). We do
NOT fetch the individual ``/job-{id}?source=1`` detail pages (they carry
a ``?`` and are disallowed) — every field we need is already on the
listing card.

Caveats (documented on purpose)
-------------------------------
* Jobsora is an **aggregator-of-aggregators** — a card's underlying
  source can itself be Jobgether/JobLeads/etc., so its rows overlap
  heavily with our other sources. The scan upsert + dedup handle exact
  dupes; cross-source near-dupes are expected.
* The ``url`` is a Jobsora **redirect** (``/job-{id}``), not the direct
  employer link.
* The robots-allowed ``/jobs-remote`` category is remote-heavy; hybrid
  is only reachable via a ``?``-filtered URL we won't crawl, so most
  rows classify as remote. A hybrid-labelled card (``gui-label-info``)
  is still honoured via ``remote_scope`` when present.

Slug convention
---------------
``{country}/{category-path}`` — e.g. ``ae/jobs-remote``. A bare country
code (``ae``) defaults to the ``jobs-remote`` category.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

# 20 cards/page × 10 = up to 200 jobs/scan. The upsert-on-external-id
# semantics mean the next scan picks up anything past the ceiling.
_MAX_PAGES = 10


class JobsoraFetcher(BaseFetcher):
    """Aggregator fetcher for Jobsora per-country listing pages."""

    PLATFORM = "jobsora"

    def fetch(self, slug: str) -> list[dict]:
        raw = (slug or "").strip().strip("/")
        if "/" in raw:
            country, path = raw.split("/", 1)
        else:
            country, path = (raw or "ae"), "jobs-remote"
        country = country.lower().strip()
        path = path.strip("/")
        if not country.isalpha() or len(country) != 2:
            logger.warning("Jobsora slug country must be ISO alpha-2; got %r", slug)
            return []

        base = f"https://{country}.jobsora.com"
        client = self._get_client()
        all_jobs: list[dict] = []
        seen: set[str] = set()

        for page in range(1, _MAX_PAGES + 1):
            url = f"{base}/{path}" if page == 1 else f"{base}/{path}-{page}"
            try:
                resp = client.get(url, headers={"Accept": "text/html"})
            except httpx.RequestError as exc:
                logger.warning("Jobsora %s page %s request failed: %s", country, page, exc)
                break
            if resp.status_code == 404:
                break  # ran past the last page
            if not resp.is_success:
                logger.warning("Jobsora %s/%s page %s -> %s", country, path, page, resp.status_code)
                break

            cards = BeautifulSoup(resp.text, "html.parser").select(
                "article.js-vacancy-snippet"
            )
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                job = self._normalize(card, country)
                if job and job["external_id"] not in seen:
                    seen.add(job["external_id"])
                    all_jobs.append(job)
                    new_on_page += 1
            # A page that adds nothing new means pagination looped back to
            # the last real page (Jobsora serves the final page for any
            # over-range ``-N``) — stop to avoid a pointless full sweep.
            if new_on_page == 0:
                break

        logger.info("Jobsora %s/%s fetched %d jobs", country, path, len(all_jobs))
        return all_jobs

    def _normalize(self, card, country: str) -> Optional[dict]:
        ext = (card.get("data-id") or "").strip()
        title_el = card.select_one(".c-job-item__title")
        title = title_el.get_text(strip=True) if title_el else ""
        url = (card.get("data-href") or "").strip()
        if not url:
            a = card.select_one(".c-job-item__title a") or card.find("a", href=True)
            url = (a.get("href") if a else "") or ""
        if not ext or not title or not url:
            return None

        # ``.c-job-item__info-item`` appears twice: [0]=company, [1]=location.
        infos = [
            e.get_text(" ", strip=True)
            for e in card.select(".c-job-item__info-item")
            if e.get_text(strip=True)
        ]
        company = infos[0] if len(infos) >= 1 else ""
        location = infos[1] if len(infos) >= 2 else ""

        # ``.gui-label-info`` carries the work-mode chip ("Remote",
        # "Hybrid", …). Only forward it as remote_scope when it's an
        # actual mode — the classifier then reads location + scope.
        label_el = card.select_one(".gui-label-info")
        mode = label_el.get_text(strip=True).lower() if label_el else ""
        remote_scope = mode if any(
            m in mode for m in ("remote", "hybrid", "on-site", "onsite", "on site")
        ) else ""

        return {
            "external_id": f"jobsora-{country}-{ext}",
            "company_slug": self._slugify(company) or f"jobsora-{country}-unknown",
            "company_name": company,
            "title": title,
            "url": url,
            "platform": self.PLATFORM,
            "location_raw": location,
            "remote_scope": remote_scope,
            "department": "",
            "employment_type": "",
            "salary_range": "",
            "posted_at": None,
            "raw_json": {
                "data_id": ext,
                "mode": mode,
                "company": company,
                "location": location,
            },
        }

    @staticmethod
    def _slugify(name: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
        return s[:200]
