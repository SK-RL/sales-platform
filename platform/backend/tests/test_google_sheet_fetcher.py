"""Unit tests for the Google Sheets fetcher (F351).

Pure-function + mocked-client tests, no network. Locks in:

  * slug parsing (full URL / bare ID / gid variants / garbage)
  * header aliasing incl. required-column enforcement with an
    actionable error naming the headers actually found
  * row normalization: stable external_id (URL-keyed vs
    company+title-keyed), skip-on-missing-required, URL fallback
    to the sheet link, remote-scope pass-through
  * private-sheet detection (HTML response → actionable error)
  * row cap
  * registration in FETCHER_MAP + PlatformFilter
"""

from __future__ import annotations

import os
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
os.environ.setdefault("JWT_SECRET", "pytest-google-sheet")


SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdef"


def _client_with_csv(csv_text: str, content_type: str = "text/csv", status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": content_type}
    resp.content = csv_text.encode("utf-8")
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = resp
    return client


# ═══ slug parsing ════════════════════════════════════════════════


class TestSlugParsing:
    def test_full_url(self):
        from app.fetchers.google_sheet import parse_sheet_slug

        sid, gid = parse_sheet_slug(
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=123"
        )
        assert sid == SHEET_ID
        assert gid == "123"

    def test_bare_id_defaults_gid_zero(self):
        from app.fetchers.google_sheet import parse_sheet_slug

        assert parse_sheet_slug(SHEET_ID) == (SHEET_ID, "0")

    def test_id_with_gid_suffix(self):
        from app.fetchers.google_sheet import parse_sheet_slug

        assert parse_sheet_slug(f"{SHEET_ID}#gid=42") == (SHEET_ID, "42")

    def test_garbage_raises(self):
        from app.fetchers.google_sheet import parse_sheet_slug

        for bad in ("", "   ", "not a sheet", "https://example.com/x", "short"):
            with pytest.raises(ValueError):
                parse_sheet_slug(bad)


# ═══ fetch + parse ═══════════════════════════════════════════════


class TestFetch:
    CSV = (
        "Job Title,Company,JD Link,Location,Work Mode,CTC,Notes\n"
        "Senior DevOps Engineer,Acme Gulf,https://acme.example/jd/1,Dubai,Remote,30 LPA,warm lead\n"
        "QA Lead,Beta LLC,,Abu Dhabi,Hybrid,,\n"
        ",MissingTitle Co,https://x.example/jd,Dubai,,,\n"
        "No Company Role,,https://y.example/jd,Dubai,,,\n"
    )

    def _fetch(self, csv_text=None, **client_kwargs):
        from app.fetchers.google_sheet import GoogleSheetFetcher

        # ``is None`` — an explicit empty string must stay empty for
        # the empty-sheet test.
        payload = self.CSV if csv_text is None else csv_text
        client = _client_with_csv(payload, **client_kwargs)
        return GoogleSheetFetcher(client=client).fetch(SHEET_ID)

    def test_ingests_valid_rows_skips_incomplete(self):
        jobs = self._fetch()
        # 2 valid rows; the title-less and company-less rows skipped.
        assert len(jobs) == 2
        titles = {j["title"] for j in jobs}
        assert titles == {"Senior DevOps Engineer", "QA Lead"}

    def test_field_mapping(self):
        job = self._fetch()[0]
        assert job["company_name"] == "Acme Gulf"
        assert job["company_slug"] == "acme-gulf"
        assert job["url"] == "https://acme.example/jd/1"
        assert job["location_raw"] == "Dubai"
        assert job["remote_scope"] == "Remote"
        assert job["salary_range"] == "30 LPA"
        assert job["platform"] == "google_sheet"

    def test_external_id_stable_and_prefixed(self):
        a = self._fetch()[0]["external_id"]
        b = self._fetch()[0]["external_id"]
        assert a == b, "external_id must be stable across scans (upsert key)"
        assert a.startswith(f"gsheet-{SHEET_ID[:8]}-")

    def test_urlless_row_links_to_sheet(self):
        qa = [j for j in self._fetch() if j["title"] == "QA Lead"][0]
        assert SHEET_ID in qa["url"]

    def test_urlless_dedup_key_uses_company_title(self):
        """Two scans of the same URL-less row → same id; a different
        title → different id."""
        qa1 = [j for j in self._fetch() if j["title"] == "QA Lead"][0]
        csv2 = self.CSV.replace("QA Lead", "QA Architect")
        qa2 = [j for j in self._fetch(csv2) if j["title"] == "QA Architect"][0]
        assert qa1["external_id"] != qa2["external_id"]

    def test_private_sheet_raises_actionable_error(self):
        with pytest.raises(ValueError, match="not link-shared"):
            self._fetch(content_type="text/html; charset=utf-8")

    def test_missing_required_headers_names_found_ones(self):
        bad_csv = "Foo,Bar,Baz\n1,2,3\n"
        with pytest.raises(ValueError, match="Headers found"):
            self._fetch(bad_csv)

    def test_row_cap(self):
        from app.fetchers import google_sheet as mod

        many = "Title,Company\n" + "\n".join(
            f"Role {i},Co {i}" for i in range(mod._MAX_ROWS + 50)
        )
        jobs = self._fetch(many)
        assert len(jobs) == mod._MAX_ROWS

    def test_empty_sheet_returns_empty(self):
        assert self._fetch("") == []

    def test_header_alias_variants(self):
        """A differently-spelled but recognisable header row maps."""
        csv2 = (
            "role,employer,apply link,city\n"
            "SRE,Gamma FZ LLC,https://g.example/a,Sharjah\n"
        )
        jobs = self._fetch(csv2)
        assert len(jobs) == 1
        assert jobs[0]["company_name"] == "Gamma FZ LLC"
        assert jobs[0]["location_raw"] == "Sharjah"


# ═══ registration ════════════════════════════════════════════════


def test_registered_in_fetcher_map_and_platform_filter():
    from typing import get_args

    from app.fetchers import FETCHER_MAP
    from app.fetchers.google_sheet import GoogleSheetFetcher
    from app.schemas.job import PlatformFilter

    assert FETCHER_MAP.get("google_sheet") is GoogleSheetFetcher
    assert "google_sheet" in get_args(PlatformFilter)
    assert GoogleSheetFetcher.PLATFORM == "google_sheet"


class TestTeamSheetHeaderShape:
    """The team's real sheets (registered 2026-07-24) use this header
    convention — lock the mapping so an alias refactor can't break
    the live boards."""

    TEAM_CSV = (
        "Date,Name of Company,Company Website,Company Email,Name,"
        "Designation,Email id,LinkedIn,Technology/Designation,"
        "Job Post Link,Location,Salary Range,Name of Freelancer,"
        "Apply Date,From Freelance/Company,Appiled From,"
        "Applies Through Form,,Response by team\n"
        "07/04/2026,Canonical,https://canonical.com/,,John Contact,"
        "Hiring Manager,hm@x.com,li.com/x,Site Reliability Engineer,"
        "https://canonical.com/careers/4468036,WFA,,,,,,,,\n"
    )

    def _fetch(self):
        from app.fetchers.google_sheet import GoogleSheetFetcher

        client = _client_with_csv(self.TEAM_CSV)
        return GoogleSheetFetcher(client=client).fetch(SHEET_ID)

    def test_title_comes_from_technology_designation_not_contact(self):
        """'Designation' (col 6) is the CONTACT's title ('Hiring
        Manager'); the job title lives in 'Technology/Designation'
        (col 9). Alias-priority matching must pick the latter even
        though the former appears in an earlier column."""
        jobs = self._fetch()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Site Reliability Engineer"
        assert jobs[0]["title"] != "Hiring Manager"

    def test_company_and_url_aliases(self):
        job = self._fetch()[0]
        assert job["company_name"] == "Canonical"
        assert job["url"] == "https://canonical.com/careers/4468036"
        assert job["location_raw"] == "WFA"


# ═══ F352 — sheet→board ATS-link promotion ═══════════════════════


class TestPromoteSheetAtsLinks:
    def test_extract_ats_ref_known_platforms(self):
        from app.workers.tasks.discovery_task import _extract_ats_ref

        cases = [
            ("https://boards.greenhouse.io/canonical/jobs/4468036", ("greenhouse", "canonical")),
            ("https://job-boards.greenhouse.io/calendly/jobs/123", ("greenhouse", "calendly")),
            ("https://jobs.lever.co/coinmarketcap/abc-def", ("lever", "coinmarketcap")),
            ("https://jobs.ashbyhq.com/DoubleZero/xyz", ("ashby", "DoubleZero")),
            ("https://apply.workable.com/covergo/j/ABC/", ("workable", "covergo")),
            ("https://vexxhost.bamboohr.com/careers/42", ("bamboohr", "vexxhost")),
            ("https://bunq.recruitee.com/o/devops", ("recruitee", "bunq")),
        ]
        for url, expected in cases:
            assert _extract_ats_ref(url) == expected, url

    def test_extract_ats_ref_rejects_non_ats_and_junk(self):
        from app.workers.tasks.discovery_task import _extract_ats_ref

        for url in (
            "",
            "https://example.com/careers",
            "https://wellfound.com/jobs/123",       # no fetcher that works
            "https://docs.google.com/spreadsheets/d/x",
            "https://boards.greenhouse.io/embed",   # denylisted segment
        ):
            assert _extract_ats_ref(url) is None, url

    def test_ashby_slug_case_preserved(self):
        """Ashby slugs are case-sensitive — lowercasing would 404 the
        board on every scan until stale-cull removed it."""
        from app.workers.tasks.discovery_task import _extract_ats_ref

        assert _extract_ats_ref("https://jobs.ashbyhq.com/DoubleZero/x") == ("ashby", "DoubleZero")

    def test_task_registered_and_scheduled(self):
        import inspect as _inspect

        from app.workers.celery_app import celery_app
        from app.workers.tasks import discovery_task

        assert callable(discovery_task.promote_sheet_ats_links)
        # Beat entry exists and points at the right task name.
        beat = celery_app.conf.beat_schedule
        entry = beat.get("promote_sheet_ats_links")
        assert entry is not None, "beat entry missing"
        assert entry["task"].endswith("promote_sheet_ats_links")
        # Guarded: per-run cap + board-existence check.
        src = _inspect.getsource(discovery_task.promote_sheet_ats_links)
        assert "limit" in src
        assert "CompanyATSBoard.platform == platform" in src
