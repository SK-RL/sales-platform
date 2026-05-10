"""Shared helpers for RSS-feed-based fetchers.

We have multiple aggregator fetchers that ingest jobs from public
RSS feeds (WeWorkRemotely, Working Nomads, planned Remote.co,
JustRemote, NoDesk, etc.). They all hit the same two parsing
quirks:

  1. ``<pubDate>`` is RFC-822 (``"Fri, 09 May 2026 18:30:00 +0000"``)
     — Postgres ``DateTime(timezone=True)`` won't coerce it; the
     value silently drops to NULL at the upsert boundary.
  2. ``<description>`` is HTML inside CDATA — the JD pipeline
     (``app/utils/job_description.py::extract_description``) reads
     ``raw_json["description"]`` so we just need to persist the
     string as-is and the existing extractor handles it.

Centralising the pubDate normaliser here so a fix-once-everywhere
change is possible. Each fetcher owns its own RSS-element-to-dict
shape (title format, company-in-title vs dc:creator, etc.) so we
don't try to over-abstract the per-feed quirks.

F335 introduced the shared module; F332 (WWR) was the first
inline implementation that motivated it.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime


def normalize_rss_pubdate(raw: str) -> str:
    """Convert an RSS ``<pubDate>`` value to ISO-8601 with timezone.

    Returns ``""`` on:
      * empty / whitespace-only input
      * unparseable RFC-822 strings
      * naive datetimes (RFC-822 input that lacks a tz offset)

    The naive-datetime drop is intentional: the alternative is to
    fabricate a UTC offset, but RSS feeds without explicit zones
    are ambiguous (could be the publisher's local time) and
    fabricating UTC would silently shift every posted_at by up to
    23 hours. The "give up rather than guess" pattern matches
    what the other (non-RSS) fetchers do for ambiguous timestamps.

    Examples:
      >>> normalize_rss_pubdate("Fri, 09 May 2026 18:30:00 +0000")
      '2026-05-09T18:30:00+00:00'
      >>> normalize_rss_pubdate("")
      ''
      >>> normalize_rss_pubdate("not a date")
      ''
    """
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        dt = parsedate_to_datetime(s)
        if dt is None or dt.tzinfo is None:
            return ""
        return dt.isoformat()
    except (TypeError, ValueError):
        return ""
