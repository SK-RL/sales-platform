"""Tests for the remote-policy redefinition (d0e1f2g3h4i5).

Pins:
  * The classifier maps every documented input pattern to the right
    ``(policy, countries)`` pair.
  * The legacy ↔ new translation table is symmetric for the four
    legacy buckets (the migration backfill + shadow-write pipeline
    both depend on this).
  * Country-code helpers reject malformed input at the boundary so
    junk never reaches the JSONB column.
  * Migration touches the right schema artefacts.
  * Job model has the new columns with the right types/defaults.

Same pattern as ``test_work_window_full.py`` — pure-function unit
tests + source-inspection guards. No live DB.
"""

from __future__ import annotations

import inspect
import os
import re
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
os.environ.setdefault("JWT_SECRET", "pytest-remote-policy")


# ═════════════════════════════════════════════════════════════════════
# Utils — labels, mappings, helpers
# ═════════════════════════════════════════════════════════════════════


class TestVocabulary:
    def test_all_six_policies_have_labels(self):
        """Every value in ``ALL_POLICIES`` must have a UI label —
        otherwise the frontend would fall back to rendering the raw
        enum value (``country_restricted``) which is exactly the
        regression we just fixed."""
        from app.utils.remote_policy import ALL_POLICIES, POLICY_LABELS

        assert set(ALL_POLICIES) == set(POLICY_LABELS.keys())
        # Sanity — labels are human-readable, not the raw enum.
        assert POLICY_LABELS["worldwide"] == "Worldwide remote"
        assert POLICY_LABELS["country_restricted"] == "Country-restricted remote"
        assert POLICY_LABELS["onsite"] == "On-site"

    def test_short_labels_match_full_set(self):
        from app.utils.remote_policy import ALL_POLICIES, POLICY_SHORT_LABELS as SHORT

        assert set(ALL_POLICIES) == set(SHORT.keys())


class TestLegacyTranslation:
    """The migration backfill + the classifier shadow-write both
    depend on these two tables being symmetric. A regression here
    would make the legacy ``geography_bucket`` column drift from the
    new ``remote_policy`` over time."""

    def test_legacy_buckets_round_trip(self):
        from app.utils.remote_policy import (
            LEGACY_TO_COUNTRIES,
            LEGACY_TO_POLICY,
            legacy_bucket_for,
        )

        for legacy in ("global_remote", "usa_only", "uae_only", ""):
            policy = LEGACY_TO_POLICY[legacy]
            countries = LEGACY_TO_COUNTRIES[legacy]
            # Round-trip: legacy → (policy, countries) → legacy
            recovered = legacy_bucket_for(policy, countries)
            # The "" → "unknown" → "" round-trip is exact; the
            # other three should also be exact.
            assert recovered == legacy, (
                f"Legacy round-trip failed for {legacy!r}: "
                f"got {recovered!r} via policy={policy!r}, "
                f"countries={countries!r}"
            )

    def test_legacy_bucket_for_handles_unknown_combos(self):
        """A multi-country country_restricted (e.g. ["US","CA"]) can't
        be represented in the legacy schema — must fall back to "".
        Same for hybrid/onsite/region_restricted."""
        from app.utils.remote_policy import legacy_bucket_for

        assert legacy_bucket_for("country_restricted", ["US", "CA"]) == ""
        assert legacy_bucket_for("country_restricted", ["IN"]) == ""
        assert legacy_bucket_for("hybrid", []) == ""
        assert legacy_bucket_for("onsite", []) == ""
        assert legacy_bucket_for("region_restricted", []) == ""
        assert legacy_bucket_for("unknown", []) == ""


class TestCountryHelpers:
    def test_normalise_country_uppers_alpha2(self):
        from app.utils.remote_policy import normalise_country

        assert normalise_country("us") == "US"
        assert normalise_country(" IN ") == "IN"

    def test_normalise_country_rejects_bad_input(self):
        from app.utils.remote_policy import normalise_country

        # Note: leading/trailing whitespace is stripped before
        # validation, so "US " is accepted (covered by the
        # ``test_normalise_country_uppers_alpha2`` test). Only
        # genuinely malformed input rejects.
        for bad in ("USA", "U", "1S", "", "U.S.", "12"):
            with pytest.raises(ValueError):
                normalise_country(bad)

    def test_normalise_countries_dedupes_and_sorts(self):
        """JSONB list shape stays stable across writes — this keeps
        the GIN index hit rate high and makes the backend's "did
        these change?" diff cheaper."""
        from app.utils.remote_policy import normalise_countries

        assert normalise_countries(["us", "IN", "us", "ca"]) == ["CA", "IN", "US"]


# ═════════════════════════════════════════════════════════════════════
# Classifier — covers every documented detection branch
# ═════════════════════════════════════════════════════════════════════


class TestClassifier:
    """Each test pins one branch of ``classify_remote_policy``. Order
    of declarations follows the order of detection in the function so
    a regression that re-orders the branches is easy to spot."""

    def test_onsite_signals_beat_everything(self):
        from app.workers.tasks._role_matching import classify_remote_policy

        # Even with "remote" in scope, an explicit "on-site only"
        # trumps. Common in scraped descriptions where the company's
        # ATS template includes both keywords.
        assert classify_remote_policy("Bangalore (on-site only)", "remote") == (
            "onsite",
            [],
        )
        assert classify_remote_policy("Mumbai", "no remote") == ("onsite", [])

    def test_hybrid_signals(self):
        from app.workers.tasks._role_matching import classify_remote_policy

        assert classify_remote_policy("Hybrid - 3 days/week in NYC", "hybrid") == (
            "hybrid",
            [],
        )
        assert classify_remote_policy("Bangalore (Hybrid)", "") == ("hybrid", [])

    def test_hybrid_retains_uae_country(self):
        """UAE sourcing push: a hybrid role that names a UAE location
        must keep the ``AE`` country code so it's findable via
        ``?remote_policy=hybrid&remote_country=AE``. Pre-fix these were
        classified as bare ``("hybrid", [])`` and vanished from every
        UAE view even though most UAE roles are hybrid, not remote."""
        from app.workers.tasks._role_matching import classify_remote_policy

        assert classify_remote_policy("Hybrid - Dubai", "hybrid") == (
            "hybrid",
            ["AE"],
        )
        assert classify_remote_policy("Dubai (Hybrid)", "") == ("hybrid", ["AE"])
        assert classify_remote_policy("Hybrid - Abu Dhabi, UAE", "") == (
            "hybrid",
            ["AE"],
        )
        # A hybrid role naming another country keeps THAT country, and a
        # bare hybrid with no country stays empty (unchanged behaviour).
        assert classify_remote_policy("Hybrid - Remote UK", "") == (
            "hybrid",
            ["GB"],
        )
        assert classify_remote_policy("Hybrid - 3 days/week in NYC", "hybrid") == (
            "hybrid",
            [],
        )

    def test_onsite_uae_not_country_tagged(self):
        """Scope guard: the UAE push targets hybrid + fully-remote only,
        NOT on-site. On-site UAE roles stay ``("onsite", [])`` so they
        don't leak into the UAE-hybrid preset."""
        from app.workers.tasks._role_matching import classify_remote_policy

        assert classify_remote_policy("On-site only - Dubai", "onsite") == (
            "onsite",
            [],
        )

    def test_strong_remote_overrides_hybrid(self):
        """A listing that says "100% remote" but also "in office"
        somewhere shouldn't silently flip to hybrid. The strong
        remote phrase wins."""
        from app.workers.tasks._role_matching import classify_remote_policy

        # 100% remote + "in office" buzzword → should NOT be hybrid.
        # Hybrid signals list contains "in office" as a weak signal,
        # so the strong-remote guard matters.
        policy, _ = classify_remote_policy(
            "Anywhere — 100% remote, no in office days", "remote"
        )
        assert policy != "hybrid"

    def test_region_restricted_signals(self):
        from app.workers.tasks._role_matching import classify_remote_policy

        for loc in ("Remote - EMEA", "Remote - APAC", "Remote, EU only"):
            policy, countries = classify_remote_policy(loc, "remote")
            assert policy == "region_restricted", (loc, policy)
            assert countries == []

    def test_country_restricted_us_and_uae(self):
        from app.workers.tasks._role_matching import classify_remote_policy

        # Legacy "USA Only" / "UAE Only" semantics preserved exactly.
        assert classify_remote_policy("Remote - US", "remote") == (
            "country_restricted",
            ["US"],
        )
        assert classify_remote_policy("Remote - UAE", "remote") == (
            "country_restricted",
            ["AE"],
        )
        assert classify_remote_policy("USA only", "") == (
            "country_restricted",
            ["US"],
        )

    def test_country_restricted_via_region_locked_table(self):
        """Region-locked single-country signals should map to
        ``country_restricted`` + the matching ISO code, not to
        ``region_restricted``. A regression here would put India-
        only / Germany-only / Brazil-only jobs in the wrong bucket."""
        from app.workers.tasks._role_matching import classify_remote_policy

        cases = [
            ("Remote - India", "IN"),
            ("Remote - Germany", "DE"),
            ("Remote - Canada", "CA"),
            ("Remote - Brazil", "BR"),
            ("Remote - Singapore", "SG"),
        ]
        for loc, code in cases:
            policy, countries = classify_remote_policy(loc, "remote")
            assert policy == "country_restricted", (loc, policy)
            assert countries == [code], (loc, countries)

    def test_worldwide_signals(self):
        from app.workers.tasks._role_matching import classify_remote_policy

        for loc in (
            "Worldwide",
            "Anywhere",
            "Remote - Anywhere",
            "Work from anywhere",
            "Fully remote",
        ):
            policy, countries = classify_remote_policy(loc, "remote")
            assert policy == "worldwide", (loc, policy)
            assert countries == []

    def test_us_company_hires_anywhere_is_worldwide(self):
        """The user-confirmed semantic: company HQ doesn't matter.
        A US company with a "remote, anywhere" posting → worldwide."""
        from app.workers.tasks._role_matching import classify_remote_policy

        policy, countries = classify_remote_policy(
            "Worldwide - based in San Francisco, US", "remote"
        )
        assert policy == "worldwide"
        assert countries == []

    def test_unknown_for_genuinely_ambiguous_input(self):
        """Empty input + no signals → unknown. NOT worldwide
        (don't promise something the listing doesn't say)."""
        from app.workers.tasks._role_matching import classify_remote_policy

        assert classify_remote_policy("", "") == ("unknown", [])
        # "Mumbai" alone — no remote signal, no on-site signal, no
        # country-only marker. Genuinely ambiguous.
        policy, _ = classify_remote_policy("Mumbai", "")
        assert policy == "unknown"

    def test_legacy_classify_geography_unchanged(self):
        """``classify_geography`` is the legacy entry point that other
        callers still depend on (scoring, exports). It must keep
        returning the legacy bucket strings — a regression here would
        break shadow-write parity AND any unmigrated frontend code."""
        from app.workers.tasks._role_matching import classify_geography

        assert classify_geography("Remote - US", "remote") == "usa_only"
        assert classify_geography("Worldwide", "remote") == "global_remote"
        assert classify_geography("Remote - UAE", "remote") == "uae_only"
        assert classify_geography("Remote - India", "remote") == ""


class TestClassifierShadowWriteContract:
    """The scan + maintenance tasks derive ``geography_bucket`` from
    ``(policy, countries)`` via ``legacy_bucket_for``. This invariant
    must hold for every classifier output.
    """

    def test_classifier_output_round_trips_to_legacy(self):
        from app.utils.remote_policy import legacy_bucket_for
        from app.workers.tasks._role_matching import (
            classify_geography,
            classify_remote_policy,
        )

        cases = [
            "Remote - US",
            "Remote - UAE",
            "Worldwide",
            "100% Remote",
            "Hybrid - NYC",
            "On-site only Bangalore",
            "Remote - EMEA",
            "",
        ]
        for loc in cases:
            policy, countries = classify_remote_policy(loc, "remote")
            shadow = legacy_bucket_for(policy, countries)
            legacy = classify_geography(loc, "remote")
            # The shadow-write bucket should equal what the legacy
            # classifier produces for any (policy, countries) pair
            # the new classifier emits — otherwise the two columns
            # diverge in the DB. Allowed exceptions: cases where the
            # NEW classifier recognises hybrid/onsite/region but the
            # legacy one returns "" (we know more in those cases —
            # the legacy column is just losing information, not
            # going wrong).
            if policy in ("worldwide", "country_restricted"):
                assert shadow == legacy, (
                    f"Shadow-write disagreement for {loc!r}: "
                    f"new=({policy},{countries}) → shadow={shadow!r} "
                    f"vs legacy={legacy!r}"
                )


# ═════════════════════════════════════════════════════════════════════
# Job model + migration — schema artefacts
# ═════════════════════════════════════════════════════════════════════


class TestJobModelColumns:
    def test_remote_policy_column_shape(self):
        from sqlalchemy import String

        from app.models.job import Job

        col = Job.__table__.c.remote_policy
        assert isinstance(col.type, String)
        assert col.type.length == 32
        assert col.nullable is False
        assert "unknown" in str(col.server_default.arg)

    def test_remote_policy_countries_is_jsonb(self):
        """JSONB (not JSON) — the GIN containment index requires it."""
        from sqlalchemy.dialects.postgresql import JSONB

        from app.models.job import Job

        col = Job.__table__.c.remote_policy_countries
        assert isinstance(col.type, JSONB)
        assert col.nullable is False
        # server_default writes "[]" — empty JSON array.
        assert "[]" in str(col.server_default.arg)

    def test_geography_bucket_kept_for_backward_compat(self):
        """The legacy column must still be on the model — old
        callers + the shadow-write pipeline depend on it. A future
        cleanup migration will drop it."""
        from app.models.job import Job

        assert "geography_bucket" in Job.__table__.c

    def test_legacy_geography_index_kept(self):
        """The legacy index covers the legacy column for the one-
        release transition window. Don't drop it before the column."""
        from app.models.job import Job

        names = {ix.name for ix in Job.__table__.indexes}
        assert "idx_jobs_geography" in names
        assert "idx_jobs_remote_policy" in names


class TestMigration:
    def _migration(self) -> str:
        versions = (
            Path(__file__).resolve().parent.parent / "alembic" / "versions"
        )
        target = next(versions.glob("*remote_scope*.py"))
        return target.read_text()

    def test_migration_adds_both_new_columns(self):
        src = self._migration()
        assert "remote_policy" in src
        assert "remote_policy_countries" in src

    def test_migration_creates_gin_index_for_jsonb(self):
        """The ``remote_country=US`` filter relies on JSONB ``@>``
        which requires a GIN index for performance. A regression that
        drops ``postgresql_using="gin"`` would silently turn the
        filter into a sequential scan on every request."""
        src = self._migration()
        assert 'postgresql_using="gin"' in src

    def test_migration_backfills_from_geography_bucket(self):
        """Legacy → new mapping must be in the backfill SQL.
        A regression where someone trims the CASE expression would
        leave existing rows stuck on ``unknown``."""
        src = self._migration()
        for legacy in ("global_remote", "usa_only", "uae_only"):
            assert legacy in src
        # Country code literals show up too.
        assert '"US"' in src
        assert '"AE"' in src

    def test_migration_is_idempotent(self):
        src = self._migration()
        assert "_column_exists" in src
        assert "_index_exists" in src


# ═════════════════════════════════════════════════════════════════════
# Schema — JobOut + filter Literal
# ═════════════════════════════════════════════════════════════════════


class TestJobOutSchema:
    def test_jobout_includes_new_fields(self):
        from app.schemas.job import JobOut

        fields = JobOut.model_fields
        assert "remote_policy" in fields
        assert "remote_policy_countries" in fields
        # Defaults — old API responses without the field shouldn't crash.
        assert fields["remote_policy"].default == "unknown"
        assert fields["remote_policy_countries"].default == []

    def test_remote_policy_filter_literal(self):
        """The query-param filter must be a Literal of all six
        policies. Frontend sends raw enum values; FastAPI 422s typos
        at parse time."""
        from typing import get_args

        from app.schemas.job import RemotePolicyFilter

        values = set(get_args(RemotePolicyFilter))
        assert values == {
            "worldwide",
            "country_restricted",
            "region_restricted",
            "hybrid",
            "onsite",
            "unknown",
        }


# ═════════════════════════════════════════════════════════════════════
# Endpoint — list_jobs accepts the new filter params
# ═════════════════════════════════════════════════════════════════════


class TestJobsEndpointFilters:
    def test_list_jobs_accepts_remote_policy_param(self):
        """A frontend that switches to the new vocabulary must be
        able to send ``?remote_policy=worldwide``. Source-level guard
        — covered by Literal at parse time, but a regression that
        drops the param entirely would fail silently."""
        from app.api.v1 import jobs

        sig = inspect.signature(jobs.list_jobs)
        assert "remote_policy" in sig.parameters
        assert "remote_country" in sig.parameters

    def test_list_jobs_filter_uses_jsonb_containment(self):
        """The country filter must use ``@>`` containment on the
        JSONB column — anything else (LIKE, indexed-text) silently
        falls back to a slow seq-scan and breaks at scale.

        Also pins the ``bindparam(..., type_=JSONB)`` form. Manual
        e2e testing on a fresh local DB caught that ``.op("@>")(
        literal)`` and ``.contains([code])`` both bind the right-side
        value as VARCHAR, producing
        ``operator does not exist: jsonb @> character varying``
        at query time. Only an explicit ``bindparam`` with
        ``type_=JSONB`` ships the parameter at the right type.
        """
        src = inspect.getsource(jobs_list_jobs())
        assert 'op("@>")' in src
        assert "bindparam(" in src
        assert "type_=JSONB" in src

    def test_list_jobs_validates_country_code(self):
        """Bad country codes must 422 at the boundary, not corrupt
        the JSONB query. Source check that the validator wraps the
        filter."""
        src = inspect.getsource(jobs_list_jobs())
        assert "normalise_country" in src
        assert "status_code=422" in src


def jobs_list_jobs():
    """Indirection so the inspect.getsource call above stays in
    one well-defined import context — the file does many late
    imports inside route handlers."""
    from app.api.v1 import jobs

    return jobs.list_jobs


# ═════════════════════════════════════════════════════════════════════
# Source guards — shadow-write contract in scan + maintenance tasks
# ═════════════════════════════════════════════════════════════════════


class TestShadowWriteSites:
    def test_scan_task_sets_remote_policy(self):
        """``scan_task._upsert_job`` must set both ``geography_bucket``
        and ``remote_policy`` on every write. A regression that drops
        one would let the columns drift apart over time."""
        scan_src = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "workers"
            / "tasks"
            / "scan_task.py"
        ).read_text()
        # Keyword-arg form on the new-job branch.
        assert "remote_policy=remote_policy" in scan_src
        assert "remote_policy_countries=remote_policy_countries" in scan_src
        # Attribute-assignment form on the update branch.
        assert "existing.remote_policy = remote_policy" in scan_src
        assert (
            "existing.remote_policy_countries = remote_policy_countries"
            in scan_src
        )
        # Legacy bucket still computed via legacy_bucket_for so the
        # two columns derive from one source of truth.
        assert "legacy_bucket_for" in scan_src

    def test_maintenance_task_updates_both(self):
        m_src = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "workers"
            / "tasks"
            / "maintenance_task.py"
        ).read_text()
        # Updates the new columns when classification changes.
        assert "job.remote_policy = new_policy" in m_src
        assert "job.remote_policy_countries = new_policy_countries" in m_src
        # Still updates the legacy column for one-release shadow-write.
        assert "job.geography_bucket = new_geo" in m_src

    def test_classify_remote_policy_exists(self):
        """The new function must be exported at module level so
        scan_task + maintenance_task can import it."""
        from app.workers.tasks._role_matching import classify_remote_policy

        assert callable(classify_remote_policy)
