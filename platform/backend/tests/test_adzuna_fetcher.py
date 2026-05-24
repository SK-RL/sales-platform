"""Unit tests for the Adzuna fetcher.

Pure-function tests using a fake httpx client — no live API calls,
no env-var dependence (we monkeypatch when needed). Locks in:

  * Soft no-op when credentials are missing
  * Pagination stops on empty results / fewer-than-page-size results
  * 401 / 429 stop the country's pagination cleanly
  * The normalize step maps Adzuna's JSON shape onto our standard dict
  * Bad slugs (non-ISO-2) are rejected up front
  * The fetcher is registered in FETCHER_MAP
  * The seed migration mentions the right artefacts
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest


os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-adzuna")


SAMPLE_RESULT = {
    "id": "5234567890",
    "title": "Senior Backend Engineer",
    "company": {"display_name": "Careem"},
    "location": {
        "area": ["UAE", "Dubai", "Dubai Marina"],
        "display_name": "Dubai Marina, Dubai",
    },
    "category": {"label": "IT Jobs", "tag": "it-jobs"},
    "contract_type": "permanent",
    "salary_min": 25000,
    "salary_max": 40000,
    "salary_is_predicted": "0",
    "redirect_url": "https://adzuna.ae/jobs/details/5234567890",
    "description": "...",
    "created": "2026-05-12T08:23:00Z",
}


def _client_returning(*pages):
    """Build a fake httpx.Client whose .get() yields the pages in order.

    Each page is either a dict (becomes a 200 response) or a tuple
    ``(status_code, payload)`` (for error-path tests). Calls past the
    last page repeat the last one — sufficient for our pagination
    loop which exits on empty/fewer-than-page-size.
    """
    pages = list(pages)
    responses = []
    for p in pages:
        resp = MagicMock()
        if isinstance(p, tuple):
            resp.status_code, payload = p
            resp.is_success = 200 <= resp.status_code < 300
        else:
            resp.status_code = 200
            resp.is_success = True
            payload = p
        resp.json.return_value = payload
        responses.append(resp)

    client = MagicMock()
    client.get.side_effect = responses + [responses[-1]] * 10
    return client


# ─── Credential + slug gates ─────────────────────────────────────


def test_missing_credentials_returns_empty_list(monkeypatch, caplog):
    """No app_id/app_key → no API call, no exception."""
    from app.fetchers.adzuna import AdzunaFetcher

    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)

    f = AdzunaFetcher(client=MagicMock(get=MagicMock(side_effect=AssertionError("should not be called"))))
    out = f.fetch("ae")
    assert out == []


def test_bad_slug_rejected(monkeypatch):
    """Slug must be a 2-letter ISO country code."""
    from app.fetchers.adzuna import AdzunaFetcher

    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")

    f = AdzunaFetcher(client=MagicMock(get=MagicMock(side_effect=AssertionError)))
    for bad in ("UAE", "ae1", "", "a", "abc", "1a"):
        assert f.fetch(bad) == [], f"slug {bad!r} should be rejected"


# ─── Pagination ──────────────────────────────────────────────────


def test_paginates_until_empty_results(monkeypatch):
    """Pulls page 1, 2, 3 until ``results`` is empty."""
    from app.fetchers.adzuna import AdzunaFetcher

    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")

    p1 = {"count": 150, "results": [dict(SAMPLE_RESULT, id=str(i)) for i in range(50)]}
    p2 = {"count": 150, "results": [dict(SAMPLE_RESULT, id=str(i)) for i in range(50, 100)]}
    p3 = {"count": 150, "results": [dict(SAMPLE_RESULT, id=str(i)) for i in range(100, 150)]}
    p4 = {"count": 150, "results": []}

    f = AdzunaFetcher(client=_client_returning(p1, p2, p3, p4))
    out = f.fetch("ae")
    # 150 rows expected; pagination stops on the empty page 4.
    assert len(out) == 150


def test_stops_when_page_is_partial(monkeypatch):
    """Last page returns fewer than ``results_per_page`` → stop."""
    from app.fetchers.adzuna import AdzunaFetcher

    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")

    # 30 < 50 (RESULTS_PER_PAGE) → loop exits after this page.
    p1 = {"count": 30, "results": [dict(SAMPLE_RESULT, id=str(i)) for i in range(30)]}
    f = AdzunaFetcher(client=_client_returning(p1))
    out = f.fetch("ae")
    assert len(out) == 30


def test_safety_ceiling_caps_pagination(monkeypatch):
    """Even if the API claims a huge count, _MAX_PAGES bounds the loop."""
    from app.fetchers.adzuna import _MAX_PAGES, AdzunaFetcher

    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")

    # Each page returns exactly RESULTS_PER_PAGE so neither the
    # partial-page exit nor the count-reached exit fires.
    pages = [
        {
            "count": 100000,
            "results": [dict(SAMPLE_RESULT, id=f"{p}-{i}") for i in range(50)],
        }
        for p in range(_MAX_PAGES + 5)
    ]
    f = AdzunaFetcher(client=_client_returning(*pages))
    out = f.fetch("ae")
    # 50 rows × _MAX_PAGES — the ceiling held.
    assert len(out) == 50 * _MAX_PAGES


def test_401_stops_pagination(monkeypatch, caplog):
    from app.fetchers.adzuna import AdzunaFetcher

    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")

    f = AdzunaFetcher(client=_client_returning((401, {"error": "auth"})))
    out = f.fetch("ae")
    assert out == []


def test_429_stops_pagination(monkeypatch):
    from app.fetchers.adzuna import AdzunaFetcher

    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")

    p1 = {"count": 100, "results": [dict(SAMPLE_RESULT, id=str(i)) for i in range(50)]}
    rate_limited = (429, {"error": "rate limited"})
    f = AdzunaFetcher(client=_client_returning(p1, rate_limited))
    out = f.fetch("ae")
    # Page 1 succeeded → 50 jobs returned; page 2 rate-limited so we stop.
    assert len(out) == 50


# ─── Normalisation ───────────────────────────────────────────────


def test_normalize_full_row():
    from app.fetchers.adzuna import AdzunaFetcher

    f = AdzunaFetcher()
    n = f._normalize(SAMPLE_RESULT, "ae")
    assert n is not None
    assert n["external_id"] == "adzuna-ae-5234567890"
    assert n["title"] == "Senior Backend Engineer"
    assert n["company_slug"] == "careem"
    assert n["company_name"] == "Careem"
    assert n["platform"] == "adzuna"
    assert n["location_raw"] == "Dubai Marina, Dubai"
    assert n["department"] == "IT Jobs"
    assert n["employment_type"] == "permanent"
    assert n["salary_range"] == "25000 - 40000"
    assert n["url"] == "https://adzuna.ae/jobs/details/5234567890"
    # raw_json preserved for downstream audit / re-parse.
    assert n["raw_json"] is SAMPLE_RESULT


def test_normalize_drops_rows_with_missing_required_fields():
    from app.fetchers.adzuna import AdzunaFetcher

    f = AdzunaFetcher()
    # missing id
    assert f._normalize({**SAMPLE_RESULT, "id": ""}, "ae") is None
    # missing title
    assert f._normalize({**SAMPLE_RESULT, "title": ""}, "ae") is None
    # missing redirect_url
    assert f._normalize({**SAMPLE_RESULT, "redirect_url": ""}, "ae") is None


def test_normalize_falls_back_to_area_for_location():
    """When ``display_name`` is absent, join ``area`` with " > "."""
    from app.fetchers.adzuna import AdzunaFetcher

    f = AdzunaFetcher()
    row = {**SAMPLE_RESULT, "location": {"area": ["UAE", "Dubai", "JLT"]}}
    n = f._normalize(row, "ae")
    assert n["location_raw"] == "UAE > Dubai > JLT"


def test_normalize_skips_predicted_salary():
    """Adzuna sometimes flags ``salary_is_predicted=1`` — don't surface."""
    from app.fetchers.adzuna import AdzunaFetcher

    f = AdzunaFetcher()
    row = {**SAMPLE_RESULT, "salary_is_predicted": "1"}
    n = f._normalize(row, "ae")
    assert n["salary_range"] == ""


def test_normalize_handles_missing_company():
    """Adzuna rows occasionally lack a company display name — fall back
    to a parking slug so the row still upserts."""
    from app.fetchers.adzuna import AdzunaFetcher

    f = AdzunaFetcher()
    row = {**SAMPLE_RESULT, "company": {}}
    n = f._normalize(row, "ae")
    assert n["company_slug"] == "adzuna-ae-unknown"
    assert n["company_name"] == ""


# ─── Slugify edge cases ──────────────────────────────────────────


def test_slugify_handles_punctuation_and_unicode():
    from app.fetchers.adzuna import AdzunaFetcher

    cases = [
        ("Careem", "careem"),
        ("Tabby Inc.", "tabby-inc"),
        ("Etisalat & e&", "etisalat-e"),
        ("ADNOC Digital", "adnoc-digital"),
        ("   Trimmed   ", "trimmed"),
        ("", ""),
    ]
    for inp, expected in cases:
        assert AdzunaFetcher._slugify(inp) == expected, inp


# ─── Registration ────────────────────────────────────────────────


def test_adzuna_in_fetcher_map():
    """A regression that drops the registration line would silently
    stop the scan from picking up Adzuna boards."""
    from app.fetchers import FETCHER_MAP
    from app.fetchers.adzuna import AdzunaFetcher

    assert FETCHER_MAP.get("adzuna") is AdzunaFetcher


def test_platform_constant_matches_map_key():
    """Defensive: the PLATFORM class attribute must match the key
    used in FETCHER_MAP, or scan_task's per-platform error tagging
    points at the wrong fetcher."""
    from app.fetchers.adzuna import AdzunaFetcher

    assert AdzunaFetcher.PLATFORM == "adzuna"


# ─── Migration probe ─────────────────────────────────────────────


def test_seed_migration_mentions_uae_board():
    versions = (
        Path(__file__).resolve().parent.parent / "alembic" / "versions"
    )
    target = next(versions.glob("*adzuna_uae_board*.py"))
    src = target.read_text()
    # The platform + country + synthetic company slug must all be
    # named. A regression that renames the platform key without
    # updating the seed would orphan the board.
    assert "'adzuna'" in src or '"adzuna"' in src
    assert "'ae'" in src or '"ae"' in src
    assert "adzuna-uae" in src
    # Idempotent — re-running the migration is a no-op.
    assert "SELECT id FROM companies WHERE slug" in src
    assert "SELECT id FROM company_ats_boards" in src


def test_env_example_documents_adzuna_keys():
    """A deploy that forgets ADZUNA_APP_ID/KEY → soft no-op (no jobs)
    but the env doc must surface the requirement so operators know
    why the board isn't pulling anything."""
    env = (
        Path(__file__).resolve().parent.parent.parent / ".env.example"
    ).read_text()
    assert "ADZUNA_APP_ID" in env
    assert "ADZUNA_APP_KEY" in env
