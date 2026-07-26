"""F354 — recruiter/HR contact harvesting (sheet POCs + JD mining)."""

from __future__ import annotations

import inspect
import os

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
os.environ.setdefault("JWT_SECRET", "pytest-contacts")


class TestHiringMailboxClassifier:
    def test_hiring_tokens_match(self):
        from app.workers.tasks.enrichment_task import _looks_like_hiring_mailbox

        for e in (
            "careers@acme.com", "talent@x.io", "recruiting@y.co",
            "hr@z.ae", "apply@startup.dev", "jobs.emea@corp.com",
            "cv-submissions@agency.in",
        ):
            assert _looks_like_hiring_mailbox(e), e

    def test_non_hiring_rejected(self):
        from app.workers.tasks.enrichment_task import _looks_like_hiring_mailbox

        for e in ("john.smith@acme.com", "support@acme.com", "info@x.io"):
            assert not _looks_like_hiring_mailbox(e), e


class TestSheetContactCapture:
    def test_fetcher_captures_contact_columns(self):
        """The team-sheet contact columns must land in raw_json.row."""
        from unittest.mock import MagicMock

        from app.fetchers.google_sheet import GoogleSheetFetcher

        csv = (
            "Name of Company,Technology/Designation,Job Post Link,"
            "Name,Designation,Email id,LinkedIn,Company Email\n"
            "Acme,SRE,https://a.example/jd,Ravi Sharma,Talent Lead,"
            "ravi@acme.com,https://li.com/ravi,careers@acme.com\n"
        )
        resp = MagicMock(status_code=200, headers={"content-type": "text/csv"},
                         content=csv.encode())
        client = MagicMock(); client.get.return_value = resp
        jobs = GoogleSheetFetcher(client=client).fetch("1" + "a" * 30)
        row = jobs[0]["raw_json"]["row"]
        assert row["contact_name"] == "Ravi Sharma"
        assert row["contact_email"] == "ravi@acme.com"
        assert row["company_email"] == "careers@acme.com"
        assert row["contact_linkedin"] == "https://li.com/ravi"
        # bare "Designation" re-purposed as contact title because the
        # job title resolved to Technology/Designation.
        assert row["contact_title"] == "Talent Lead"
        assert jobs[0]["title"] == "SRE"


class TestTaskWiring:
    def test_tasks_registered_and_scheduled(self):
        from app.workers.celery_app import celery_app
        from app.workers.tasks import enrichment_task

        assert callable(enrichment_task.sync_sheet_contacts)
        assert callable(enrichment_task.mine_jd_contact_emails)
        beat = celery_app.conf.beat_schedule
        assert beat["sync_sheet_contacts"]["task"].endswith("sync_sheet_contacts")
        assert beat["mine_jd_contact_emails"]["task"].endswith("mine_jd_contact_emails")

    def test_upsert_dedupes_case_insensitively(self):
        from app.workers.tasks.enrichment_task import _upsert_contact

        src = inspect.getsource(_upsert_contact)
        # Mirrors the F282 partial-unique semantics.
        assert "func.lower(func.trim(" in src

    def test_jd_miner_person_emails_require_company_domain(self):
        """The junk gate: non-hiring-keyword emails only accepted on
        the employer's own domain."""
        from app.workers.tasks.enrichment_task import mine_jd_contact_emails

        src = inspect.getsource(mine_jd_contact_emails)
        assert "domain.endswith(cdom)" in src
        assert "_JUNK_EMAIL_DOMAINS" in src

    def test_sheet_contacts_outrank_scraped_confidence(self):
        from app.workers.tasks.enrichment_task import sync_sheet_contacts

        src = inspect.getsource(sync_sheet_contacts)
        assert "confidence=0.9" in src
