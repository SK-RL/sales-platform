"""Adzuna fetcher — public job-board aggregator with country filtering.

Adzuna exposes a clean, well-documented JSON API at
``https://api.adzuna.com/v1/api/jobs/{country}/search/{page}``. Country
codes follow ISO-3166 alpha-2 (e.g. ``ae`` for UAE, ``gb`` for UK,
``us`` for US). Authentication is an ``app_id`` + ``app_key`` pair
issued for free at https://developer.adzuna.com/ — the free tier
caps at 1000 calls/month, which is plenty for a daily UAE sync (one
country × ~20 pages = 20 calls/day → ~600/month with headroom).

This is the highest-ROI sustainable source for UAE jobs we evaluated:

  * Bayt, Naukrigulf, GulfTalent all 403 our user-agent (Cloudflare /
    DataDome bot protection) and explicitly disallow scrapers in
    robots.txt.
  * Himalayas has a ``location`` query param but it's silently ignored
    by the upstream API — a UAE-filtered call returns Japan/US/UK
    rows. We're already pulling everything from Himalayas via
    ``__all__`` and classifying downstream, which surfaces ~185 UAE
    jobs out of 105k worldwide.
  * Adzuna's ``/jobs/ae/`` path returns only UAE-located postings and
    the JSON shape is consistent across countries, so a future
    ``/jobs/in/`` (India) or ``/jobs/sa/`` (Saudi) add is a one-line
    config change rather than a new fetcher.

Slug convention — ``"ae"``, ``"us"``, ``"gb"`` etc. The fetcher uses
the slug directly as the country code. The boards table seeds one
row per (country, "adzuna") pair; the scan picks it up exactly like
any other ATS board.

Per-country freshness window: the free tier caps how aggressively we
can refresh. We page through up to ``_MAX_PAGES`` pages of 50 results
each (so worst-case 1000 jobs per scan); the next scan picks up new
postings via the upsert-on-external-id logic in scan_task.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

# Country-scoped search endpoint. ``{page}`` is 1-indexed; pagination
# stops once ``results`` is empty OR we hit the safety ceiling.
API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

# 50 is the documented max per page on the free tier; lower numbers
# burn the monthly quota faster for the same total coverage.
RESULTS_PER_PAGE = 50

# Safety ceiling — caps a single scan at 50 × 20 = 1000 jobs. Adzuna
# UAE typically has 3-8k live postings, so a daily scan won't capture
# everything in one run; the upsert-on-external-id semantics mean the
# next run picks up what we missed. Trade-off: keeps the free-tier
# quota healthy (20 calls/scan × ~30 days = 600/month, well under
# the 1000 monthly cap with headroom for retries).
_MAX_PAGES = 20


class AdzunaFetcher(BaseFetcher):
    """Aggregator fetcher for the Adzuna public job-board API.

    Slug == ISO-3166 alpha-2 country code (``ae``, ``us``, ``gb``,
    ``in``, ``sa``...). The pre-seeded UAE board uses ``ae``.

    Credentials come from the env (``ADZUNA_APP_ID`` and
    ``ADZUNA_APP_KEY``). When unset the fetcher returns an empty list
    and logs a single warning per scan — the scan pipeline treats it
    as a board with no current postings rather than failing the
    whole sweep. Production deploys must set both env vars in the
    GitHub Actions secrets that the deploy workflow forwards into
    ``platform/.env`` (see ``ci-deploy.sh::persist_adzuna_keys``).
    """

    PLATFORM = "adzuna"

    def fetch(self, slug: str) -> list[dict]:
        app_id = os.getenv("ADZUNA_APP_ID", "").strip()
        app_key = os.getenv("ADZUNA_APP_KEY", "").strip()
        if not app_id or not app_key:
            # Soft-fail — surface once at WARN so monitoring catches
            # the missing creds without 500ing the whole scan task.
            # The /api/health endpoint exposes a separate `adzuna_
            # configured` boolean for the deploy-verify step.
            logger.warning(
                "Adzuna credentials missing (ADZUNA_APP_ID / "
                "ADZUNA_APP_KEY); skipping country=%s", slug,
            )
            return []

        country = (slug or "").strip().lower()
        if not country or len(country) != 2 or not country.isalpha():
            logger.warning(
                "Adzuna slug must be an ISO-3166 alpha-2 country code; "
                "got %r — skipping",
                slug,
            )
            return []

        client = self._get_client()
        all_jobs: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            url = API_URL.format(country=country, page=page)
            try:
                resp = client.get(
                    url,
                    params={
                        "app_id": app_id,
                        "app_key": app_key,
                        "results_per_page": RESULTS_PER_PAGE,
                        # ``content-type=application/json`` is the
                        # default; explicit for paranoia.
                        "content-type": "application/json",
                    },
                )
            except httpx.RequestError as exc:
                logger.warning(
                    "Adzuna %s page %s request failed: %s",
                    country, page, exc,
                )
                break

            if resp.status_code == 401:
                # Likely revoked / misconfigured credentials. Log once
                # and abort the country's pagination — re-running with
                # bad creds would just burn the page-1 quota.
                logger.error(
                    "Adzuna %s returned 401 — check ADZUNA_APP_ID / "
                    "ADZUNA_APP_KEY in deploy secrets",
                    country,
                )
                break
            if resp.status_code == 429:
                # Rate-limited (free tier = 1000 calls/month). Stop
                # pagination so we don't keep hammering the API; the
                # next scheduled scan will resume from page 1.
                logger.warning(
                    "Adzuna %s rate-limited (429) at page %s — stopping",
                    country, page,
                )
                break
            if not resp.is_success:
                logger.warning(
                    "Adzuna %s page %s returned %s",
                    country, page, resp.status_code,
                )
                break

            data = resp.json()
            results = data.get("results") or []
            if not results:
                break

            for raw in results:
                normalized = self._normalize(raw, country)
                if normalized:
                    all_jobs.append(normalized)

            # Adzuna returns ``count`` (total matches across all pages)
            # but no explicit hasMore flag. We stop when we've pulled
            # ``count`` rows OR when the current page returns fewer
            # than ``results_per_page`` (the last page).
            total = data.get("count", 0) or 0
            if len(all_jobs) >= total or len(results) < RESULTS_PER_PAGE:
                break

        return all_jobs

    def _normalize(self, raw: dict[str, Any], country: str) -> Optional[dict]:
        """Map an Adzuna result dict to the platform's normalized shape.

        Adzuna shape (relevant fields):
          {
            "id": "5278...",                    # globally-unique string
            "title": "Senior Backend Engineer",
            "company": {"display_name": "Careem"},
            "location": {
                "area": ["UAE", "Dubai", "Dubai Marina"],
                "display_name": "Dubai Marina, Dubai"
            },
            "category": {"label": "IT Jobs", "tag": "it-jobs"},
            "contract_type": "permanent",       # may be missing
            "salary_min": 25000, "salary_max": 40000, "salary_is_predicted": "1",
            "redirect_url": "https://adzuna.../jobs/details/...",
            "description": "Long text...",
            "created": "2026-05-12T08:23:00Z",
          }

        Returns ``None`` when the row is missing mandatory fields (id,
        title, url) — the scan task skips ``None`` entries silently.
        """
        external_id = str(raw.get("id", "")).strip()
        title = (raw.get("title") or "").strip()
        url = (raw.get("redirect_url") or "").strip()
        if not external_id or not title or not url:
            return None

        company = raw.get("company") or {}
        company_name = (company.get("display_name") or "").strip()
        # Adzuna doesn't expose a stable company slug — derive one from
        # the display name so multiple postings from the same employer
        # collapse onto one Company row in our DB. Lowercase, dasherize,
        # strip non-alphanum. Empty when the row has no company → use
        # ``adzuna-{country}-unknown`` as a parking slug.
        company_slug = self._slugify(company_name) or f"adzuna-{country}-unknown"

        # Location handling — Adzuna's ``display_name`` is the most
        # human-readable form ("Dubai Marina, Dubai"). ``area`` is the
        # hierarchy (e.g. ["UAE", "Dubai", "Dubai Marina"]).
        location = raw.get("location") or {}
        location_raw = (
            location.get("display_name")
            or " > ".join(location.get("area") or [])
            or ""
        )

        # ``remote_scope`` is the freeform raw-text field downstream
        # classifier reads. Adzuna doesn't have a remote/hybrid/onsite
        # flag on the row — we leave it empty and let the classifier
        # work from the title + description + location string. If the
        # description mentions "remote" the classifier will pick it
        # up; otherwise the job classifies as country_restricted with
        # the country code derived from the board's slug (the
        # `classify_remote_policy` heuristic recognises UAE/US/UAE/etc
        # location text).
        remote_scope = ""

        # Salary — only forwarded when both bounds are present, in the
        # ATS-row convention "min - max" (string). Adzuna sometimes
        # returns a predicted salary (`salary_is_predicted=1`); we
        # don't surface predictions to avoid misleading the user.
        salary_range = ""
        if (
            raw.get("salary_min")
            and raw.get("salary_max")
            and str(raw.get("salary_is_predicted", "0")) == "0"
        ):
            salary_range = f"{int(raw['salary_min'])} - {int(raw['salary_max'])}"

        return {
            "external_id": f"adzuna-{country}-{external_id}",
            "company_slug": company_slug,
            "company_name": company_name,
            "title": title,
            "url": url,
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": remote_scope,
            "department": (raw.get("category") or {}).get("label", ""),
            "employment_type": (raw.get("contract_type") or "").strip(),
            "salary_range": salary_range,
            "posted_at": raw.get("created"),
            "raw_json": raw,
        }

    @staticmethod
    def _slugify(name: str) -> str:
        """Conservative slugifier — lowercase, drop punctuation, join
        with dashes. Stable enough that the same employer always maps
        to the same slug across scans (Careem → ``careem``, "Tabby
        Inc." → ``tabby-inc``)."""
        import re

        s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return s[:200]  # bound the column width
