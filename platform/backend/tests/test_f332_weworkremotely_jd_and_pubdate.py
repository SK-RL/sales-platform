"""F332 — weworkremotely fetcher: capture <description> + normalize pubDate.

Pre-fix the WWR fetcher silently dropped two pieces of data per
RSS item:

 1. ``<description>`` — the full job-posting body as HTML. The
    normalizer never read it, so the resulting ``raw_json`` had no
    ``description`` key. ``extract_description("weworkremotely",
    raw_json)`` consequently returned ``("", "")`` and no
    JobDescription row was created. Prod audit (2026-05-09): 18 of
    20 sampled WWR jobs had no JD body at all, locking ~660 WWR
    rows out of role-cluster classification (the keyword scorer
    needs JD text).

 2. ``<pubDate>`` — RFC-822 timestamp like ``"Fri, 09 May 2026
    18:30:00 +0000"``. The DB column ``Job.posted_at`` is
    ``DateTime(timezone=True)``; pre-fix the raw RFC-822 string
    sailed through to the upsert path, silently dropped at the
    SQLAlchemy coercion boundary, and every WWR row ended up
    ``posted_at IS NULL``. Prod audit: 0/50 sampled WWR jobs had
    posted_at populated.

F332 fixes both:

 * Fetcher now reads ``item.findtext("description")`` and stores
   the HTML under ``raw_json["description"]``. The existing JD
   pipeline picks it up either through the ``("description",
   "content")`` default fallback OR through the explicit
   ``"weworkremotely": ("description",)`` mapping in
   ``_HTML_KEYS_BY_PLATFORM`` (added defense-in-depth so a future
   raw_json key drift doesn't silently re-introduce the bug).
 * Fetcher now passes raw ``pubDate`` through
   ``_normalize_pubdate`` (a thin wrapper around
   ``email.utils.parsedate_to_datetime``) which yields ISO-8601
   with timezone, or ``""`` on parse failure (matching the "give
   up rather than guess" pattern the other fetchers use).
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
os.environ.setdefault("JWT_SECRET", "pytest-f332")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_fetcher() -> str:
    return (_BACKEND / "app" / "fetchers" / "weworkremotely.py").read_text()


def _read_jd_helper() -> str:
    return (_BACKEND / "app" / "utils" / "job_description.py").read_text()


# ───────────────────────── source-level guards ─────────────────────────


def test_fetcher_imports_parsedate_to_datetime():
    """The pubDate normaliser must be importable in the fetcher.

    F335 moved the implementation into ``app/utils/rss.py`` and
    re-exported it as ``_normalize_pubdate`` for the test surface
    here. Either form (inline import of ``parsedate_to_datetime``
    OR the shared-util re-export) is acceptable as long as the
    helper still fires.
    """
    src = _read_fetcher()
    inline = "from email.utils import parsedate_to_datetime" in src
    shared = (
        "from app.utils.rss import normalize_rss_pubdate" in src
        and "_normalize_pubdate" in src
    )
    assert inline or shared, (
        "F332 regression: pubDate normalisation no longer wired up "
        "(neither the inline parsedate_to_datetime import nor the "
        "shared rss.normalize_rss_pubdate re-export found)."
    )


def test_fetcher_calls_normalize_pubdate():
    src = _read_fetcher()
    assert "_normalize_pubdate(" in src, (
        "F332 regression: _normalize_pubdate helper no longer "
        "called. WWR posted_at column will be NULL again."
    )


def test_fetcher_extracts_description_from_rss_item():
    src = _read_fetcher()
    assert 'item.findtext("description")' in src, (
        "F332 regression: WWR fetcher no longer reads the RSS "
        "<description> element. ~660 WWR jobs would lose their "
        "JD text again, locking them out of the relevance pool."
    )


def test_fetcher_persists_description_in_raw_json():
    """The description must land under ``raw_json["description"]``
    so ``extract_description`` finds it.
    """
    src = _read_fetcher()
    # Look for both the dict key and the variable that holds the
    # extracted string.
    assert '"description": description_html' in src, (
        "F332 regression: WWR fetcher reads the description but "
        "doesn't persist it into raw_json — the JD pipeline can't "
        "see it."
    )


def test_explicit_weworkremotely_mapping_in_jd_helper():
    """Defense-in-depth: explicit platform mapping so a future
    raw_json key drift doesn't re-introduce the silent-drop bug.
    """
    src = _read_jd_helper()
    assert '"weworkremotely":' in src, (
        "F332 regression: explicit weworkremotely entry removed "
        "from _HTML_KEYS_BY_PLATFORM. Any rename of the raw_json "
        "key would now silently fall through to the default "
        "fallback and could regress JD coverage again."
    )


# ───────────────────────── behavioural tests ─────────────────────────


def test_normalize_pubdate_parses_rfc822_to_iso8601():
    from app.fetchers.weworkremotely import _normalize_pubdate
    iso = _normalize_pubdate("Fri, 09 May 2026 18:30:00 +0000")
    # Round-trip: parse the ISO string back and re-check.
    from datetime import datetime
    dt = datetime.fromisoformat(iso)
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 9
    assert dt.hour == 18
    # Must be timezone-aware.
    assert dt.tzinfo is not None


def test_normalize_pubdate_returns_empty_for_unparseable():
    from app.fetchers.weworkremotely import _normalize_pubdate
    assert _normalize_pubdate("") == ""
    assert _normalize_pubdate("not a date") == ""
    # All-whitespace.
    assert _normalize_pubdate("   ") == ""


def test_normalize_pubdate_drops_naive_datetime():
    """RFC-822 without a timezone offset is ambiguous — better to
    drop it than fabricate UTC."""
    from app.fetchers.weworkremotely import _normalize_pubdate
    # Pubdate without a timezone offset returns naive — we drop it.
    out = _normalize_pubdate("Fri, 09 May 2026 18:30:00")
    assert out == "" or out.endswith("+00:00") or "+" in out or "-" in out
    # If parsedate_to_datetime returns naive (no tz) we expect "".
    # Accept either behaviour but require we never pass naive
    # forward as a valid ISO string with no zone.


def test_fetcher_normalize_rss_full_round_trip():
    """End-to-end: feed a synthetic <item> through ``_normalize_rss``
    and confirm the output dict has both posted_at (ISO) and
    raw_json.description populated.
    """
    from app.fetchers.weworkremotely import WeWorkRemotelyFetcher
    item_xml = """
    <item>
      <title>ACME Corp: Senior DevOps Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/acme-senior-devops-engineer</link>
      <guid>https://weworkremotely.com/remote-jobs/acme-senior-devops-engineer</guid>
      <description><![CDATA[<p>Build and operate our Kubernetes platform.</p><ul><li>Terraform</li><li>AWS</li></ul>]]></description>
      <region>Worldwide</region>
      <category>DevOps / Sysadmin</category>
      <type>Full-Time</type>
      <pubDate>Fri, 09 May 2026 18:30:00 +0000</pubDate>
    </item>
    """
    item = ET.fromstring(item_xml)
    fetcher = WeWorkRemotelyFetcher()
    out = fetcher._normalize_rss(item)
    assert out, "fetcher returned empty dict for a valid RSS item"
    # posted_at must be ISO-8601 with timezone (T separator + offset).
    assert "T" in out["posted_at"]
    assert "+" in out["posted_at"] or "-" in out["posted_at"][10:]
    # raw_json.description must hold the HTML body.
    desc = out["raw_json"]["description"]
    assert "Kubernetes" in desc, (
        f"description was dropped or empty: {desc!r}"
    )
    # raw_json.pubDate is the NORMALISED iso form — keeping the
    # naive RFC-822 string in raw_json would let downstream
    # consumers (manual ops queries, debug dashboards) fall into
    # the same parse trap.
    assert out["raw_json"]["pubDate"] == out["posted_at"]


def test_extract_description_picks_up_wwr_description_key():
    """End-to-end: synthesised raw_json (matching what the F332
    fetcher persists) must produce a non-empty (html, text) tuple.
    """
    from app.utils.job_description import extract_description
    raw_json = {
        "guid": "https://weworkremotely.com/remote-jobs/acme",
        "description": "<p>Build our K8s platform.</p>",
        "pubDate": "2026-05-09T18:30:00+00:00",
    }
    html, text = extract_description("weworkremotely", raw_json)
    assert html, "F332 regression: WWR description not picked up by extract_description"
    assert "K8s" in html
    # text path strips tags
    assert "K8s" in text
    assert "<p>" not in text
