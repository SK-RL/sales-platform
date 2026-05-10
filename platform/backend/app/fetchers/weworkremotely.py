"""Fetch remote jobs from We Work Remotely public RSS feed.

The JSON API requires authentication, but the RSS feed is public.
Endpoint: GET https://weworkremotely.com/remote-jobs.rss
Returns XML RSS with job items. Title format: "Company: Job Title".
"""

import logging
import hashlib
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

RSS_URL = "https://weworkremotely.com/remote-jobs.rss"


def _normalize_pubdate(raw: str) -> str:
    """Convert RSS RFC-822 pubDate to ISO-8601.

    F332 regression fix: ``pubDate`` comes through as
    ``"Fri, 09 May 2026 18:30:00 +0000"`` (RFC 822 / 2822). The DB
    column is ``DateTime(timezone=True)`` and the upsert path
    expects ISO-8601 — pre-fix, the raw RFC-822 string was passed
    straight through and silently dropped at the SQLAlchemy
    coercion boundary, so every WWR row ended up with
    ``posted_at IS NULL``. Audit on prod showed 0/50 sampled WWR
    jobs had ``posted_at`` populated even though every RSS item
    carries a pubDate. Returns ``""`` on parse failure (matching
    the other fetchers' "give up rather than guess" pattern).
    """
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        dt = parsedate_to_datetime(s)
        # parsedate_to_datetime can return a naive datetime if the
        # input lacks a timezone — that's untestable for "is this
        # really UTC?", so we drop these rather than fabricate a
        # zone. All real WWR pubDates carry +0000.
        if dt is None or dt.tzinfo is None:
            return ""
        return dt.isoformat()
    except (TypeError, ValueError):
        return ""


class WeWorkRemotelyFetcher(BaseFetcher):
    """Fetch open positions from We Work Remotely via RSS."""

    PLATFORM = "weworkremotely"

    def fetch(self, slug: str) -> list[dict]:
        """Fetch all remote jobs from WWR RSS feed.

        slug is ignored (always fetches all). Use '__all__' by convention.
        """
        client = self._get_client()
        all_jobs = []

        try:
            resp = client.get(
                RSS_URL,
                headers={"User-Agent": "JobPlatform/1.0 (job aggregator)"},
            )
            if resp.status_code != 200:
                logger.warning("WWR RSS returned %s", resp.status_code)
                return []

            root = ET.fromstring(resp.content)
            items = root.findall(".//item")

            for item in items:
                normalized = self._normalize_rss(item)
                if normalized:
                    all_jobs.append(normalized)

        except Exception as exc:
            logger.warning("WWR fetch failed: %s", exc)

        logger.info("WWR fetched %d jobs from RSS", len(all_jobs))
        return all_jobs

    def _normalize_rss(self, item: ET.Element) -> dict:
        raw_title = (item.findtext("title") or "").strip()
        if not raw_title:
            return {}

        # Title format: "Company: Job Title" or "Company: Job Title | Extra"
        if ": " in raw_title:
            company_name, title = raw_title.split(": ", 1)
            company_name = company_name.strip()
            title = title.strip()
        else:
            company_name = ""
            title = raw_title

        if not title:
            return {}

        company_slug = company_name.lower().replace(" ", "-")[:100] if company_name else "unknown"

        # Location
        location_raw = (item.findtext("region") or "").strip()

        # URL
        job_url = item.findtext("link") or item.findtext("guid") or ""

        # Remote scope
        remote_scope = self._detect_remote_scope(location_raw, title) or "remote"

        # External ID from guid URL or hash
        guid = item.findtext("guid") or ""
        if guid:
            # Extract slug from URL like ".../remote-jobs/company-job-title"
            ext_id = guid.rstrip("/").rsplit("/", 1)[-1]
        else:
            ext_id = hashlib.md5(f"{company_name}-{title}".encode()).hexdigest()[:16]

        # Category as department
        department = item.findtext("category") or ""

        # Employment type
        employment_type = item.findtext("type") or ""

        # F332: RSS pubDate is RFC-822 ("Fri, 09 May 2026 18:30:00
        # +0000"). The DB column is ``DateTime(timezone=True)`` so
        # we MUST normalize to ISO-8601 here — pre-fix, the raw
        # RFC-822 string sailed through to the upsert path and
        # silently dropped, leaving every WWR row with
        # ``posted_at IS NULL``. Prod audit confirmed: 0/50
        # sampled WWR rows had posted_at populated.
        raw_pubdate = item.findtext("pubDate") or ""
        posted_at = _normalize_pubdate(raw_pubdate)

        # F332: capture ``<description>`` from the RSS item. WWR's
        # RSS includes a per-item description block with the full
        # JD as HTML; pre-fix the fetcher silently dropped it, so
        # ``raw_json`` had no description key, ``extract_description
        # ("weworkremotely", raw_json)`` returned ``("", "")``,
        # and no JobDescription row was ever created. Net effect:
        # ~660 WWR jobs in the relevance pool with zero text for
        # the keyword scorer to chew on, locking ~6% of the table
        # into ``role_cluster=EMPTY``. Storing under the key
        # ``"description"`` so the existing fallback path
        # ``_HTML_KEYS_BY_PLATFORM.get(platform, ("description",
        # "content"))`` in app/utils/job_description.py picks it
        # up without a per-platform mapping.
        description_html = (item.findtext("description") or "").strip()

        return {
            "external_id": f"wwr-{ext_id}",
            "company_slug": company_slug,
            "company_name": company_name,
            "title": title,
            "url": job_url,
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": department,
            "employment_type": employment_type,
            "salary_range": "",
            "posted_at": posted_at,
            "raw_json": {
                "guid": guid,
                "title": raw_title,
                "region": location_raw,
                "category": department,
                "type": employment_type,
                "link": job_url,
                "pubDate": posted_at,
                # F332: persist the description so the JD pipeline
                # can pick it up via extract_description.
                "description": description_html,
            },
        }
