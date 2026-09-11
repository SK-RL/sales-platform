"""F372 — standing instructions: never auto-apply when the title says X.

Subtractive only, like Tsenta's. A relevance score cannot know what a
person will not do — "DevSecOps Engineer - Clearance Required" scored
93 on a live candidate list — so the sweep honours a per-user list of
title keywords before it creates anything.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import app.workers.tasks.auto_apply_task as aat
from app.schemas.routine import RoutinePreferences
from tests.test_f360_auto_apply_sweep import FakeSession, Row, _user, wired  # noqa: F401


def _job(title, score=95):
    return Row(id=uuid.uuid4(), company_id=uuid.uuid4(), platform="greenhouse", title=title,
               status="new", relevance_score=score, geography_bucket="global_remote",
               role_cluster="infra", posted_at=datetime.now(timezone.utc) - timedelta(days=1))


class TestRule:
    def test_substring_case_insensitive(self):
        assert aat.title_excluded("DevSecOps Engineer - Clearance Required", ["clearance"])
        assert aat.title_excluded("Staff SRE (Contract)", ["CONTRACT"])

    def test_no_keywords_excludes_nothing(self):
        assert not aat.title_excluded("Anything", [])
        assert not aat.title_excluded("Anything", None)

    def test_empty_keyword_never_matches_everything(self):
        assert not aat.title_excluded("Anything", [""])


class TestPreferences:
    def test_default_is_empty(self):
        assert RoutinePreferences().excluded_title_keywords == []

    def test_entries_are_trimmed_deduped_and_capped(self):
        p = RoutinePreferences(excluded_title_keywords=["  Clearance ", "clearance", "", "x" * 100])
        assert p.excluded_title_keywords == ["Clearance", "x" * 60]


class TestSweep:
    def test_excluded_titles_are_skipped(self, wired):
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=5, excluded_title_keywords=["clearance", "contract"])],
            [_job("DevSecOps Engineer - Clearance Required"), _job("Platform Engineer (Contract)"), _job("Cloud Infrastructure Engineer")],
        )
        out = aat.sweep_auto_apply()
        assert out["queued"] == 1
        (app_row,) = wired["session"].added
        job_ids = {j.id for j in wired["session"].jobs if "Cloud" in j.title}
        assert app_row.job_id in job_ids

    def test_no_instruction_means_no_filtering(self, wired):
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=5)],
            [_job("DevSecOps Engineer - Clearance Required")],
        )
        assert aat.sweep_auto_apply()["queued"] == 1
