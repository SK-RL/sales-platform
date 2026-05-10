"""F335 — new Working Nomads RSS aggregator fetcher.

Working Nomads (https://www.workingnomads.com/) is a curated
remote-jobs aggregator. The 2026-05-09 audit recommended adding
it as the cheapest tier-1 win because:

  * Public RSS at https://www.workingnomads.com/jobsrss (no auth)
  * Strong infra / devops / SRE cohort
  * Same shape as the WWR fetcher we just hardened in F332,
    so the cost is mostly templating

Wire-up:
  1. New fetcher class at app/fetchers/workingnomads.py
  2. Added to PlatformFilter Literal in schemas/job.py
  3. Added to _AGGREGATOR_PLATFORMS in scan_task.py
  4. Added to _HTML_KEYS_BY_PLATFORM in utils/job_description.py
  5. Seed entry in seed_remote_companies.py (single board with
     slug='__all__')

Tests cover:
  * Fetcher class is importable + has the expected PLATFORM tag
  * RSS-item normalisation: end-to-end with a synthetic <item>
  * Company-name extraction cascade (dc:creator first, then
    description-body regex, then 'unknown' fallback)
  * pubDate normalisation reuses the shared
    app.utils.rss.normalize_rss_pubdate (no per-fetcher RFC-822
    parsing)
  * Skip-empty: missing title or link → empty dict (skip), not
    a half-formed row that breaks the upsert path
  * Wire-up: PlatformFilter Literal, _AGGREGATOR_PLATFORMS,
    _HTML_KEYS_BY_PLATFORM, seed list all updated together
    (atomic addition)
  * extract_description round-trip: synthesised raw_json shape
    actually produces a non-empty (html, text) tuple
"""
from __future__ import annotations

import os
import pathlib
from xml.etree import ElementTree as ET

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f335")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_BACKEND / rel).read_text()


# ────────────────── existence + identity ──────────────────


def test_fetcher_module_exists_and_class_importable():
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    assert WorkingNomadsFetcher.PLATFORM == "workingnomads"


def test_fetcher_subclasses_basefetcher():
    """Required for the registry / scan_task to discover and
    instantiate this fetcher correctly. Pre-F335 only fetchers
    extending BaseFetcher were picked up by the scanner.
    """
    from app.fetchers.base import BaseFetcher
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    assert issubclass(WorkingNomadsFetcher, BaseFetcher)


# ────────────────── pubDate normalisation reuse ──────────────────


def test_fetcher_uses_shared_pubdate_helper():
    """F335 introduced ``app/utils/rss.py``; the WN fetcher must
    import from it instead of re-implementing parsedate_to_datetime
    locally (so a fix-once-everywhere change is possible).
    """
    src = _read("app/fetchers/workingnomads.py")
    assert "from app.utils.rss import normalize_rss_pubdate" in src, (
        "F335 regression: WN fetcher no longer uses the shared "
        "RSS pubDate helper. Risk of per-fetcher drift on the same "
        "RFC-822 quirk WWR tripped over."
    )


# ────────────────── wire-up tests ──────────────────


def test_workingnomads_in_platform_filter_literal():
    from typing import get_args
    from app.schemas.job import PlatformFilter
    assert "workingnomads" in get_args(PlatformFilter), (
        "F335 regression: ``workingnomads`` removed from "
        "PlatformFilter Literal — admin scan trigger and the "
        "platform filter dropdown will 422 on the value."
    )


def test_workingnomads_in_aggregator_platforms_set():
    src = _read("app/workers/tasks/scan_task.py")
    # The set is defined as a constant in the scanner. Source-
    # level grep is enough — the runtime build of the set is
    # straight-line code.
    assert '"workingnomads"' in src, (
        "F335 regression: ``workingnomads`` removed from "
        "_AGGREGATOR_PLATFORMS. The scanner won't apply per-job "
        "company resolution, so every job gets stuck under the "
        "synthetic 'Working Nomads' company row."
    )


def test_workingnomads_in_jd_platform_map():
    src = _read("app/utils/job_description.py")
    assert '"workingnomads":' in src, (
        "F335 regression: ``workingnomads`` removed from the "
        "explicit _HTML_KEYS_BY_PLATFORM mapping. Falls back to "
        "the default ``(\"description\", \"content\")`` which "
        "still works today, but a future raw_json key drift would "
        "silently regress JD coverage. Defense-in-depth lost."
    )


def test_workingnomads_seed_entry_present():
    src = _read("app/seed_remote_companies.py")
    assert '"platform": "workingnomads"' in src, (
        "F335 regression: WN seed entry removed from "
        "seed_remote_companies.py — fresh deploys get a "
        "registered fetcher with no board to scan."
    )
    # And confirm slug='__all__' (aggregator pattern, not a per-
    # company slug).
    assert '"slug": "__all__"' in src


# ────────────────── normalisation behaviour ──────────────────


_SAMPLE_RSS_ITEM = """
<item xmlns:dc="http://purl.org/dc/elements/1.1/">
  <title>Senior DevOps Engineer (Kubernetes)</title>
  <link>https://www.workingnomads.com/jobs/senior-devops-engineer-acme-corp</link>
  <guid>https://www.workingnomads.com/jobs/senior-devops-engineer-acme-corp</guid>
  <description><![CDATA[<p><strong>ACME Corp</strong> is hiring a Senior DevOps Engineer.</p><ul><li>Terraform</li><li>AWS / GCP</li><li>Kubernetes at scale</li></ul>]]></description>
  <category>Worldwide</category>
  <category>DevOps</category>
  <category>Full-Time</category>
  <pubDate>Fri, 09 May 2026 18:30:00 +0000</pubDate>
  <dc:creator>ACME Corp</dc:creator>
</item>
"""


def test_normalize_rss_full_round_trip_uses_dc_creator():
    """Happy path: ``<dc:creator>`` present → company_name is
    populated, posted_at is ISO-8601, raw_json carries the
    description and the category list.
    """
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    item = ET.fromstring(_SAMPLE_RSS_ITEM)
    out = WorkingNomadsFetcher()._normalize_rss(item)

    assert out, "WN fetcher rejected a well-formed RSS item"
    assert out["platform"] == "workingnomads"
    assert out["title"] == "Senior DevOps Engineer (Kubernetes)"
    assert out["company_name"] == "ACME Corp"
    assert out["company_slug"] == "acme-corp"
    assert out["url"].startswith("https://www.workingnomads.com/")

    # ISO-8601 with timezone offset.
    assert "T" in out["posted_at"]
    assert out["posted_at"].endswith("+00:00") or "+" in out["posted_at"][10:]

    # JD body persisted under raw_json["description"] for the
    # extract_description pipeline.
    desc = out["raw_json"]["description"]
    assert "ACME Corp" in desc
    assert "Kubernetes" in desc

    # Category list preserved (department picks the first; full
    # list available for downstream).
    assert out["raw_json"]["categories"] == ["Worldwide", "DevOps", "Full-Time"]
    assert out["department"] == "Worldwide"
    # Worldwide signal upgrades remote_scope to "worldwide".
    assert out["remote_scope"] == "worldwide"


def test_normalize_rss_company_extraction_falls_back_to_description():
    """When ``<dc:creator>`` is missing, the fetcher should pull
    the company from the description-body opener
    ("X is hiring..." / "X is looking for...").
    """
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    no_creator_xml = """
    <item>
      <title>Senior DevOps Engineer</title>
      <link>https://www.workingnomads.com/jobs/x</link>
      <guid>https://www.workingnomads.com/jobs/x</guid>
      <description><![CDATA[<strong>BetaCloud</strong> is hiring a Senior DevOps Engineer.]]></description>
      <pubDate>Fri, 09 May 2026 18:30:00 +0000</pubDate>
    </item>
    """
    item = ET.fromstring(no_creator_xml)
    out = WorkingNomadsFetcher()._normalize_rss(item)
    assert out["company_name"] == "BetaCloud"


def test_normalize_rss_company_falls_back_to_unknown():
    """Defense-in-depth: if neither ``<dc:creator>`` nor a
    description-body match fires, ``company_slug`` still gets a
    usable string (``"unknown"``) so the upsert path doesn't
    crash on an empty company key.
    """
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    nothing_xml = """
    <item>
      <title>Some role</title>
      <link>https://www.workingnomads.com/jobs/y</link>
      <guid>https://www.workingnomads.com/jobs/y</guid>
      <description><![CDATA[An anonymous job description with nothing useful.]]></description>
      <pubDate>Fri, 09 May 2026 18:30:00 +0000</pubDate>
    </item>
    """
    item = ET.fromstring(nothing_xml)
    out = WorkingNomadsFetcher()._normalize_rss(item)
    assert out["company_slug"] == "unknown"
    # company_name is the empty string in this case — that's the
    # signal the upsert path uses to NOT bind to a real Company
    # row.
    assert out["company_name"] == ""


def test_normalize_rss_skips_items_missing_title_or_link():
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    no_title = ET.fromstring("<item><link>https://x</link></item>")
    no_link = ET.fromstring("<item><title>X</title></item>")
    f = WorkingNomadsFetcher()
    assert f._normalize_rss(no_title) == {}
    assert f._normalize_rss(no_link) == {}


def test_extract_description_picks_up_workingnomads_key():
    """End-to-end: the JD pipeline finds the description body
    in the raw_json shape that the fetcher persists.
    """
    from app.utils.job_description import extract_description
    raw_json = {
        "guid": "https://www.workingnomads.com/jobs/x",
        "description": "<p>Scale our Kubernetes platform.</p>",
        "categories": ["Worldwide", "DevOps"],
        "pubDate": "2026-05-09T18:30:00+00:00",
    }
    html, text = extract_description("workingnomads", raw_json)
    assert html, "F335 regression: WN description not picked up"
    assert "Kubernetes" in html
    assert "Kubernetes" in text
    assert "<p>" not in text  # tags stripped on the text side
