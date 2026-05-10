"""F335 — new Working Nomads JSON aggregator fetcher.

Working Nomads (https://www.workingnomads.com/) is a curated
remote-jobs aggregator. The 2026-05-09 audit recommended adding
it as the cheapest tier-1 win for the global_remote pool.

Endpoint: https://www.workingnomads.com/api/exposed_jobs/ —
public JSON array (~30-50 active jobs), no auth, no pagination.
Each row carries ``title``, ``company_name``, ``category_name``,
``location``, ``tags``, ``description`` (HTML), ``url``,
``pub_date`` (ISO-8601 with timezone, no RFC-822 parsing
needed).

Wire-up:
  1. New fetcher class at app/fetchers/workingnomads.py
  2. Added to PlatformFilter Literal in schemas/job.py
  3. Added to _AGGREGATOR_PLATFORMS in scan_task.py
  4. Added to _HTML_KEYS_BY_PLATFORM in utils/job_description.py
  5. Seed entry in seed_remote_companies.py (single board with
     slug='__all__')

Tests cover:
  * Fetcher class is importable + has the expected PLATFORM tag
  * Fetcher subclasses BaseFetcher (registry visibility)
  * Wire-up: PlatformFilter Literal, _AGGREGATOR_PLATFORMS,
    _HTML_KEYS_BY_PLATFORM, seed list all updated together
  * JSON-row normalisation: end-to-end with a synthetic dict
  * URL → external_id derivation: extracts the numeric id from
    /job/go/<id>/ paths so re-scans are idempotent
  * Skip-empty: missing title or url → empty dict (skip), not
    a half-formed row that breaks the upsert path
  * extract_description round-trip: synthesised raw_json shape
    actually produces a non-empty (html, text) tuple
"""
from __future__ import annotations

import os
import pathlib

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
    instantiate this fetcher correctly."""
    from app.fetchers.base import BaseFetcher
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    assert issubclass(WorkingNomadsFetcher, BaseFetcher)


def test_fetcher_uses_documented_endpoint():
    """The endpoint URL should be the documented JSON one
    (``/api/exposed_jobs/``), not the legacy RSS form which now
    redirects to the SPA.
    """
    src = _read("app/fetchers/workingnomads.py")
    assert "/api/exposed_jobs/" in src, (
        "F335 regression: WN fetcher endpoint reverted to a "
        "form that no longer returns JSON. The /jobsrss path "
        "now redirects to the HTML SPA — verified 2026-05-09."
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
    assert '"slug": "__all__"' in src


# ────────────────── normalisation behaviour ──────────────────


_SAMPLE_ROW = {
    "title": "Senior DevOps Engineer (Kubernetes)",
    "company_name": "ACME Corp",
    "category_name": "DevOps",
    "location": "Worldwide",
    "tags": "kubernetes,terraform,aws,gcp",
    "description": (
        "<p><strong>ACME Corp</strong> is hiring a Senior DevOps "
        "Engineer.</p><ul><li>Terraform</li><li>Kubernetes at scale</li></ul>"
    ),
    "url": "https://www.workingnomads.com/job/go/1588652/",
    "pub_date": "2026-05-08T15:54:48-04:00",
}


def test_normalize_full_round_trip():
    """Happy path: dict in, canonical fetcher output dict out.
    pub_date passes through unchanged (already ISO-8601),
    description ends up under raw_json so extract_description
    finds it.
    """
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    out = WorkingNomadsFetcher()._normalize(dict(_SAMPLE_ROW))

    assert out, "WN fetcher rejected a well-formed row"
    assert out["platform"] == "workingnomads"
    assert out["title"] == "Senior DevOps Engineer (Kubernetes)"
    assert out["company_name"] == "ACME Corp"
    assert out["company_slug"] == "acme-corp"
    assert out["url"].startswith("https://www.workingnomads.com/")

    # pub_date passes through unchanged.
    assert out["posted_at"] == "2026-05-08T15:54:48-04:00"

    # JD body persisted under raw_json["description"].
    desc = out["raw_json"]["description"]
    assert "ACME Corp" in desc
    assert "Kubernetes" in desc

    # Tag list normalised (split on commas, stripped).
    assert out["raw_json"]["tag_list"] == [
        "kubernetes", "terraform", "aws", "gcp"
    ]

    # Worldwide signal upgrades remote_scope.
    assert out["remote_scope"] == "worldwide"

    # Department comes from category_name.
    assert out["department"] == "DevOps"


def test_normalize_external_id_extracted_from_url():
    """The numeric id at the end of /job/go/<id>/ becomes the
    external_id suffix so re-scans are idempotent.
    """
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    out = WorkingNomadsFetcher()._normalize(dict(_SAMPLE_ROW))
    assert out["external_id"] == "workingnomads-1588652"


def test_normalize_external_id_falls_back_to_url_hash():
    """If the URL shape ever changes (no trailing numeric id),
    we hash the URL so the row still gets a deterministic id.
    """
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    row = dict(_SAMPLE_ROW)
    row["url"] = "https://workingnomads.com/some-other-shape"
    out = WorkingNomadsFetcher()._normalize(row)
    # Must still produce a stable, prefixed external_id.
    assert out["external_id"].startswith("workingnomads-")
    # And it shouldn't be the empty fallback.
    assert len(out["external_id"]) > len("workingnomads-")


def test_normalize_company_falls_back_to_unknown():
    """Defense-in-depth: if ``company_name`` is missing,
    ``company_slug`` still gets a usable string (``"unknown"``)
    so the upsert path doesn't crash on an empty company key.
    """
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    row = dict(_SAMPLE_ROW)
    row["company_name"] = ""
    out = WorkingNomadsFetcher()._normalize(row)
    assert out["company_slug"] == "unknown"
    assert out["company_name"] == ""


def test_normalize_skips_rows_missing_title_or_url():
    from app.fetchers.workingnomads import WorkingNomadsFetcher
    f = WorkingNomadsFetcher()
    no_title = dict(_SAMPLE_ROW); no_title["title"] = ""
    no_url = dict(_SAMPLE_ROW); no_url["url"] = ""
    assert f._normalize(no_title) == {}
    assert f._normalize(no_url) == {}


def test_extract_description_picks_up_workingnomads_key():
    """End-to-end: the JD pipeline finds the description body
    in the raw_json shape that the fetcher persists.
    """
    from app.utils.job_description import extract_description
    raw_json = {
        "url": "https://www.workingnomads.com/job/go/1588652/",
        "description": "<p>Scale our Kubernetes platform.</p>",
        "tags": "kubernetes,terraform",
        "pub_date": "2026-05-08T15:54:48-04:00",
    }
    html, text = extract_description("workingnomads", raw_json)
    assert html, "F335 regression: WN description not picked up"
    assert "Kubernetes" in html
    assert "Kubernetes" in text
    assert "<p>" not in text  # tags stripped on the text side
