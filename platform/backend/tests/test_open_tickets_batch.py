"""Regression locks for the July-2026 open-tickets batch.

Covers three infrastructure bugs + two features shipped together:

  B1  workingnomads registered in FETCHER_MAP (was: 21 scan
      errors/week "No fetcher for platform: workingnomads")
  B2  scan upsert recovers from external_id UNIQUE races (was: 53
      LinkedIn duplicate-key scan errors/week)
  B3  discovery run row committed up-front + per-source isolation
      (was: 5 nights of silent crashes, zero DiscoveryRun trace)
  F1  interview-question repository (ticket 8ef0e9c2)
  F2  manual pipeline cards (ticket bac45b42)

Style matches the existing suite: pure-import checks + source-level
guards (see test_force_change_password.py rationale) — fast, no live
DB. The features were additionally verified end-to-end against a
real Postgres before shipping (422/201/409/204 paths).
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

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
os.environ.setdefault("JWT_SECRET", "pytest-open-tickets")


# ═══ B1 — workingnomads registration ═════════════════════════════


def test_workingnomads_registered_in_fetcher_map():
    """The fetcher file + PlatformFilter entry + seeded board all
    existed, but the FETCHER_MAP entry was missed — every scan tick
    errored and the source produced zero jobs."""
    from app.fetchers import FETCHER_MAP
    from app.fetchers.workingnomads import WorkingNomadsFetcher

    assert FETCHER_MAP.get("workingnomads") is WorkingNomadsFetcher


def test_every_platformfilter_value_has_a_fetcher():
    """The generalised B1 lesson: a PlatformFilter value without a
    FETCHER_MAP entry means boards can exist that no scan can serve.
    Lock the invariant so the next new platform can't repeat it.

    (The reverse is fine — career_page etc. can be in the map without
    being a filterable job source.)
    """
    from typing import get_args

    from app.fetchers import FETCHER_MAP
    from app.schemas.job import PlatformFilter

    missing = [p for p in get_args(PlatformFilter) if p not in FETCHER_MAP]
    assert not missing, (
        f"PlatformFilter values with no registered fetcher: {missing}. "
        f"Seeded boards for these platforms will error every scan tick."
    )


# ═══ B2 — external_id dup-key recovery in scan upsert ════════════


def test_scan_upsert_recovers_external_id_unique_violation():
    src = Path(
        Path(__file__).resolve().parent.parent
        / "app" / "workers" / "tasks" / "scan_task.py"
    ).read_text()
    # Both constraints handled by the SAVEPOINT recovery...
    assert "jobs_external_id_key" in src, (
        "scan_task no longer recovers external_id UNIQUE races — "
        "concurrent LinkedIn board scans will log dup-key errors again."
    )
    assert "uq_jobs_active_company_title" in src
    # ...and the external_id branch re-fetches by the violated key.
    assert "is_external_id_dup" in src


# ═══ B3 — discovery visibility + per-source isolation ════════════


class TestDiscoveryResilience:
    def _src(self):
        from app.workers.tasks import discovery_task

        return inspect.getsource(discovery_task.discover_and_add_boards)

    def test_run_row_committed_before_crawl(self):
        """The run row must be committed BEFORE the crawl starts so a
        crash leaves a visible status='running' row (swept to failed
        by fix_stuck_discovery_runs) instead of vanishing in the
        rollback — that invisibility hid 5 nights of failures."""
        src = self._src()
        run_add = src.find("session.add(run)")
        first_commit = src.find("session.commit()", run_add)
        first_crawl = src.find("_crawl_greenhouse_sitemap")
        assert run_add > 0 and first_commit > 0 and first_crawl > 0
        assert first_commit < first_crawl, (
            "DiscoveryRun row is no longer committed before the crawl — "
            "a crash mid-crawl erases the run and failures go invisible."
        )

    def test_each_source_isolated(self):
        """One failing source (sitemap block, probe timeout) must not
        kill the whole run — partial discovery beats none."""
        src = self._src()
        assert "source_errors" in src
        assert src.count("except Exception") >= 4, (
            "Expected per-source try/except around sitemap, platform "
            "probes, and linkedin + the outer handler."
        )

    def test_failure_persisted_on_run_row(self):
        src = self._src()
        assert 'run.status = "failed"' in src
        assert "completed_with_errors" in src


# ═══ F2 — manual pipeline cards (ticket bac45b42) ════════════════


class TestManualPipelineCard:
    def test_route_registered(self):
        from app.api.v1.router import api_router

        paths = {
            (m, r.path)
            for r in api_router.routes
            for m in (getattr(r, "methods", None) or set())
        }
        assert ("POST", "/api/v1/pipeline/manual") in paths

    def test_mandatory_fields_enforced(self):
        """The ticket's four 'restricted' fields must be required —
        everything else optional."""
        from pydantic import ValidationError

        from app.schemas.pipeline import ManualPipelineCardRequest

        # All four present → parses.
        ManualPipelineCardRequest(
            company_name="Acme",
            company_website="https://acme.example",
            jd_link="https://acme.example/jd",
            applied_id="me@example.com",
        )
        # Each one missing → 422.
        base = dict(
            company_name="Acme",
            company_website="https://acme.example",
            jd_link="https://acme.example/jd",
            applied_id="me@example.com",
        )
        for missing in base:
            payload = {k: v for k, v in base.items() if k != missing}
            with pytest.raises(ValidationError):
                ManualPipelineCardRequest(**payload)

    def test_unknown_fields_rejected(self):
        from pydantic import ValidationError

        from app.schemas.pipeline import ManualPipelineCardRequest

        with pytest.raises(ValidationError):
            ManualPipelineCardRequest(
                company_name="Acme",
                company_website="https://a.example",
                jd_link="https://a.example/jd",
                applied_id="x",
                totally_unknown_field="boom",  # type: ignore[call-arg]
            )

    def test_model_and_out_schema_carry_manual_card(self):
        from app.models.pipeline import PotentialClient
        from app.schemas.pipeline import PipelineItemOut

        assert "manual_card" in PotentialClient.__table__.c
        assert "manual_card" in PipelineItemOut.model_fields

    def test_endpoint_requires_admin_or_reviewer(self):
        from app.api.v1 import pipeline as mod

        src = inspect.getsource(mod.create_manual_pipeline_card)
        assert 'require_role("admin", "reviewer")' in src

    def test_company_backfill_never_overwrites_scraped_data(self):
        """Presence wins: manual card creation must not clobber a
        scraped company's website/linkedin with hand-typed values."""
        from app.api.v1 import pipeline as mod

        src = inspect.getsource(mod.create_manual_pipeline_card)
        assert "if not company.website" in src
        assert "if not company.linkedin_url" in src


# ═══ F1 — interview question repository (ticket 8ef0e9c2) ════════


class TestInterviewQuestions:
    EXPECTED = (
        ("GET", "/api/v1/interview-questions"),
        ("POST", "/api/v1/interview-questions"),
        ("GET", "/api/v1/interview-questions/{set_id}"),
        ("PATCH", "/api/v1/interview-questions/{set_id}"),
        ("DELETE", "/api/v1/interview-questions/{set_id}"),
    )

    def test_routes_registered(self):
        from app.api.v1.router import api_router

        actual = {
            (m, r.path)
            for r in api_router.routes
            for m in (getattr(r, "methods", None) or set())
        }
        for m, p in self.EXPECTED:
            assert (m, p) in actual, f"missing {m} {p}"

    def test_author_survives_user_deletion(self):
        """SET NULL, not CASCADE — debriefs are institutional memory
        and must outlive the recording user's account."""
        from app.models.interview_question import InterviewQuestionSet

        fk = next(iter(InterviewQuestionSet.__table__.c.user_id.foreign_keys))
        assert fk.ondelete == "SET NULL"

    def test_edit_gated_to_author_or_admin(self):
        from app.api.v1 import interview_questions as mod

        src = inspect.getsource(mod._require_author_or_admin)
        assert "row.user_id != user.id" in src
        assert '"admin"' in src and '"super_admin"' in src
        # F185 — generic 403 message, no role name leak.
        assert "Insufficient privileges" in src

    def test_search_escapes_like_metachars(self):
        """`%`/`_` in a search term must not degenerate into
        wildcards (same class as findings 84/85 on /jobs)."""
        from app.api.v1 import interview_questions as mod

        src = inspect.getsource(mod.list_question_sets)
        assert "escape_like" in src

    def test_create_requires_core_fields(self):
        from pydantic import ValidationError

        from app.api.v1.interview_questions import InterviewQuestionSetCreate

        InterviewQuestionSetCreate(
            company_name="Acme",
            job_role="QA",
            interview_round="HR",
            questions="Why QA?",
        )
        with pytest.raises(ValidationError):
            InterviewQuestionSetCreate(
                company_name="Acme", job_role="QA", interview_round="HR",
                questions="",  # empty questions rejected
            )


# ═══ Migration artefacts ═════════════════════════════════════════


def test_migration_covers_both_features():
    versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    target = next(versions.glob("*interview_repo_manual_cards*.py"))
    src = target.read_text()
    assert "interview_question_sets" in src
    assert "manual_card" in src
    # Idempotent — inspector-guard pattern.
    assert "_column_exists" in src and "_table_exists" in src
    # Chains off the Adzuna seed (current head).
    assert 'down_revision = "o1p2q3r4s5t6"' in src


def test_frontend_pieces_exist():
    front = Path(__file__).resolve().parent.parent.parent / "frontend" / "src"
    assert (front / "pages" / "InterviewQuestionsPage.tsx").exists()
    app_tsx = (front / "App.tsx").read_text()
    assert "/interview-questions" in app_tsx
    sidebar = (front / "components" / "Sidebar.tsx").read_text()
    assert "/interview-questions" in sidebar
    pipeline_page = (front / "pages" / "PipelinePage.tsx").read_text()
    assert "createManualPipelineCard" in pipeline_page


# ═══ F350 — UAE sourcing push ════════════════════════════════════


class TestUAESourcingPush:
    """Expanded UAE signals + seeded UAE-hub employer boards."""

    UAE_LOCATIONS = [
        ("Sharjah, UAE", "remote"),
        ("Remote - Ras Al Khaimah", "remote"),
        ("DIFC, Dubai", ""),
        ("Dubai Internet City", ""),
        ("Based in UAE (remote)", "remote"),
        ("Abu Dhabi-based", ""),
        ("Al Ain", ""),
        ("Masdar City, Abu Dhabi", ""),
    ]

    def test_expanded_signals_classify_as_uae(self):
        from app.workers.tasks._role_matching import (
            classify_geography,
            classify_remote_policy,
        )

        for loc, scope in self.UAE_LOCATIONS:
            assert classify_geography(loc, scope) == "uae_only", loc
            policy, countries = classify_remote_policy(loc, scope)
            assert (policy, countries) == ("country_restricted", ["AE"]), loc

    def test_no_false_positive_on_ai_engineer(self):
        """'al ain' is a substring hazard — 'Principal AI Engineer'
        must not classify as UAE."""
        from app.workers.tasks._role_matching import classify_geography

        assert classify_geography("Principal AI Engineer - Remote US", "remote") == "usa_only"
        assert classify_geography("Berlin, Germany", "") == ""

    def test_uae_board_seed_migration(self):
        from pathlib import Path

        versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
        target = next(versions.glob("*uae_employer_boards*.py"))
        src = target.read_text()
        # The four probed-live boards, all on greenhouse.
        for slug in ("careem", "okx", "bybit", "tamara"):
            assert f'"{slug}"' in src, f"missing board seed: {slug}"
        # Introspected column lists (post-relevance_score-incident
        # defence) + per-board existence guard.
        assert "inspector.get_columns" in src
        assert "SELECT id FROM company_ats_boards" in src
        assert 'down_revision = "p2q3r4s5t6u7"' in src
