"""F335 — Working Nomads RSS fetcher.

Working Nomads (https://www.workingnomads.com/) is a curated
remote-jobs aggregator with a strong infra/devops/SRE cohort.
The public RSS feed at https://www.workingnomads.com/jobsrss
serves the full active list, no auth needed.

Same shape as F332's WeWorkRemotely fetcher: RSS XML →
per-item dict, RFC-822 ``<pubDate>`` normalised to ISO-8601 via
the shared ``app.utils.rss.normalize_rss_pubdate`` helper, full
HTML body persisted under ``raw_json["description"]`` so the
existing JD pipeline (``extract_description("workingnomads",
raw_json)``) finds it without per-platform mapping (the default
fallback ``("description", "content")`` already handles this).
We add an explicit entry to ``_HTML_KEYS_BY_PLATFORM`` anyway,
defense-in-depth against future drift, matching the F332
pattern.

Title format: WN's RSS items have just the role title in the
``<title>`` element; the company name lives in
``<dc:creator>`` or sometimes only in the description body.
We try ``dc:creator`` first, fall back to a regex on the
description, then fall back to ``"unknown"`` so the upsert
path still has a usable ``company_slug``.

Aggregator semantics: ``fetch(slug)`` ignores the slug and
always pulls everything (use ``slug='__all__'`` by convention,
matching WWR / Himalayas / RemoteOK / Remotive). Wire-up:

  1. Add this module to the platform registry — no-op since
     the registry derives from class subclasses of BaseFetcher.
  2. Add ``"workingnomads"`` to ``schemas/job.py:PlatformFilter``
     so admin scan triggers + filter UIs can reach it.
  3. Add ``"workingnomads"`` to ``scan_task._AGGREGATOR_PLATFORMS``
     so the scanner knows to use the ``__all__`` slug semantics.
  4. Seed a single board row (``platform='workingnomads',
     slug='__all__'``) on first deploy via a migration or the
     existing seed_remote_companies-style runner.
"""

import logging
import re
from typing import Any
from xml.etree import ElementTree as ET

from app.fetchers.base import BaseFetcher
from app.utils.rss import normalize_rss_pubdate

logger = logging.getLogger(__name__)

# Public RSS endpoint. Stable for years; verified via
# ``curl https://www.workingnomads.com/jobsrss``. Returns full
# active job list as RSS 2.0 with Dublin Core extension
# (``xmlns:dc="http://purl.org/dc/elements/1.1/"``) for
# ``<dc:creator>``.
RSS_URL = "https://www.workingnomads.com/jobsrss"

# Dublin Core namespace for ``<dc:creator>`` (which holds the
# company name when present). Pre-registered so ElementTree's
# findtext matches the prefixed element correctly without
# manually rewriting the tag string on every read.
_DC_NS = {"dc": "http://purl.org/dc/elements/1.1/"}

# Fallback regex for company extraction when ``<dc:creator>`` is
# absent — Working Nomads' description bodies typically open
# with ``"<strong>Headline:</strong>"`` followed by
# ``"<strong>Company Name</strong>\nis hiring..."``. Conservative
# pattern: only extract if the description text begins with a
# clear company-name signature.
_COMPANY_FROM_DESC_RE = re.compile(
    r"^\s*(?:<[^>]+>\s*)*([A-Za-z0-9][A-Za-z0-9 &\.\-,'!]{1,60})"
    r"\s*(?:</[^>]+>\s*)*(?:is hiring|is looking|wants|needs)\b",
    re.IGNORECASE,
)


def _company_from_description(description_html: str) -> str:
    """Extract a company name from the HTML body when
    ``<dc:creator>`` is missing. Returns ``""`` if no clear match.
    """
    if not description_html:
        return ""
    m = _COMPANY_FROM_DESC_RE.search(description_html)
    if not m:
        return ""
    name = m.group(1).strip()
    # Trim trailing words that aren't part of the company name —
    # WN sometimes opens with "ACME Corp Engineering team is
    # hiring" where "Engineering team" is descriptive, not part
    # of the name. Keep things conservative: cap at 4 words.
    parts = name.split()
    return " ".join(parts[:4]) if parts else ""


class WorkingNomadsFetcher(BaseFetcher):
    """Fetch open positions from Working Nomads via the public RSS feed."""

    PLATFORM = "workingnomads"

    def fetch(self, slug: str) -> list[dict]:
        """Fetch all WN jobs from the RSS feed.

        ``slug`` is ignored. Convention: pass ``"__all__"`` from
        the scanner so the per-board scan-log row's slug column
        has a meaningful value (matches WWR / Himalayas / RemoteOK).
        """
        client = self._get_client()
        all_jobs: list[dict] = []

        try:
            resp = client.get(
                RSS_URL,
                headers={"User-Agent": "JobPlatform/1.0 (job aggregator)"},
            )
            if resp.status_code != 200:
                logger.warning("Working Nomads RSS returned %s", resp.status_code)
                return []

            root = ET.fromstring(resp.content)
            items = root.findall(".//item")

            for item in items:
                normalized = self._normalize_rss(item)
                if normalized:
                    all_jobs.append(normalized)

        except Exception as exc:
            logger.warning("Working Nomads fetch failed: %s", exc)

        logger.info("Working Nomads fetched %d jobs from RSS", len(all_jobs))
        return all_jobs

    def _normalize_rss(self, item: ET.Element) -> dict:
        """Map a single RSS ``<item>`` to the canonical fetcher
        output shape. Returns ``{}`` (skip) for items missing
        critical fields (title, link).
        """
        title = (item.findtext("title") or "").strip()
        if not title:
            return {}

        # Working Nomads RSS uses standard <link> for the
        # job-detail URL. <guid> usually echoes the link.
        job_url = (item.findtext("link") or item.findtext("guid") or "").strip()
        if not job_url:
            return {}

        guid = (item.findtext("guid") or job_url).strip()

        # Description body — full HTML inside CDATA. Persist as-is
        # under ``raw_json["description"]`` so
        # ``extract_description`` finds it.
        description_html = (item.findtext("description") or "").strip()

        # Company resolution — try Dublin Core first, then
        # description heuristic, then "unknown".
        company_name = (
            (item.findtext("dc:creator", namespaces=_DC_NS) or "").strip()
            or _company_from_description(description_html)
            or ""
        )
        company_slug = (
            company_name.lower().replace(" ", "-").replace("/", "-")[:100]
            if company_name
            else "unknown"
        )

        # Working Nomads tags categories under <category> (often
        # multiple). First non-empty wins for ``department``;
        # we keep the full list under raw_json for downstream
        # consumers (intelligence, role-cluster auditing).
        categories = [
            (c.text or "").strip()
            for c in item.findall("category")
            if (c.text or "").strip()
        ]
        department = categories[0] if categories else ""

        # WN often tags items as "remote" + a regional qualifier
        # (e.g. "Anywhere", "Europe Only", "USA Only"). Expose
        # both raw signals to the geography classifier; the
        # downstream `geography_bucket` derivation in scan_task
        # decides global_remote vs usa_only vs uae_only based on
        # location_raw.
        location_raw = ""
        for cat in categories:
            cl = cat.lower()
            if any(kw in cl for kw in ("anywhere", "worldwide", "global", "europe", "usa", "us only")):
                location_raw = cat
                break
        if not location_raw:
            location_raw = "Remote"

        # F335: pubDate is RFC-822, same as WWR. Normalise to
        # ISO-8601 via the shared helper or land NULL on the DB.
        posted_at = normalize_rss_pubdate(item.findtext("pubDate") or "")

        # External ID: prefer the trailing slug from the URL so
        # re-scans are idempotent (same job → same ext_id).
        ext_id_tail = guid.rstrip("/").rsplit("/", 1)[-1] or guid
        ext_id = f"workingnomads-{ext_id_tail[:200]}"

        # Detect remote scope using the inherited helper. Working
        # Nomads is by definition remote, but we still pass the
        # location text + title so "Worldwide" upgrades to the
        # ``worldwide`` scope tag instead of plain ``remote``.
        remote_scope = self._detect_remote_scope(location_raw, title) or "remote"

        return {
            "external_id": ext_id,
            "company_slug": company_slug,
            "company_name": company_name,
            "title": title,
            "url": job_url,
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": department,
            "employment_type": "",  # WN's RSS doesn't surface this consistently
            "salary_range": "",  # WN's RSS doesn't surface this consistently
            "posted_at": posted_at,
            "raw_json": {
                "guid": guid,
                "title": title,
                "link": job_url,
                "categories": categories,
                "pubDate": posted_at,
                "description": description_html,
            },
        }
