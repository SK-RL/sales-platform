"""F360 — the sweeper that makes apply automatic.

Everything before this was assisted: a human picked a job, an endpoint
enqueued it. This is the loop that finds work on its own.

Because it sends real applications to real employers under the user's
name, with nobody watching, the tests below are mostly about what it
REFUSES to do. The default posture is the important one: a user who has
not explicitly opted in and set a cap must never have an application
sent for them.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import app.workers.tasks.auto_apply_task as aat
from app.schemas.routine import RoutinePreferences


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar(self):
        return self._rows[0] if self._rows else 0

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Dispatches by the entity being selected; records what was added."""

    def __init__(self, users, jobs, existing_app_job_ids=(), submitted_count=0, killed=None):
        self.users, self.jobs = users, jobs
        self.existing = list(existing_app_job_ids)
        self.submitted_count = submitted_count
        self.killed = killed
        self.added, self.commits, self.closed = [], 0, False

    def execute(self, stmt):
        desc = stmt.column_descriptions[0]
        entity = desc.get("entity")
        name = getattr(entity, "__name__", "") if entity is not None else ""
        expr = str(stmt).lower()
        if "count(" in expr:
            return _Result([self.submitted_count])
        if name == "User":
            return _Result(self.users)
        if name == "RoutineKillSwitch":
            return _Result([self.killed] if self.killed else [])
        if name == "Application":
            return _Result(self.existing)
        if name == "Job":
            return _Result(self.jobs)
        return _Result([])

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _user(**prefs):
    return Row(
        id=uuid.uuid4(),
        active_resume_id=uuid.uuid4(),
        routine_preferences=RoutinePreferences(**prefs).model_dump(mode="json"),
    )


def _job(score=95):
    return Row(
        id=uuid.uuid4(), company_id=uuid.uuid4(), platform="greenhouse",
        status="new", relevance_score=score, geography_bucket="global_remote",
        role_cluster="infra", posted_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


@pytest.fixture
def wired(monkeypatch):
    state = {"enqueued": []}

    def _session():
        return state["session"]

    monkeypatch.setattr(aat, "SyncSession", _session)

    class FakeTask:
        @staticmethod
        def delay(app_id):
            state["enqueued"].append(app_id)

    import app.workers.tasks.apply_task as apply_task
    monkeypatch.setattr(apply_task, "submit_application_task", FakeTask)
    return state


class TestDefaultPostureIsOff:
    def test_a_user_who_never_opted_in_gets_nothing(self, wired):
        wired["session"] = FakeSession([_user()], [_job()])
        out = aat.sweep_auto_apply()
        assert out["queued"] == 0
        assert wired["enqueued"] == []

    def test_enabled_without_a_cap_still_does_nothing(self, wired):
        """Flipping one switch must not start applying — the cap is a
        second, deliberate act."""
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=0)], [_job()]
        )
        assert aat.sweep_auto_apply()["queued"] == 0

    def test_defaults_are_off_and_conservative(self):
        p = RoutinePreferences()
        assert p.auto_apply_enabled is False
        assert p.auto_apply_daily_cap == 0
        # Higher than the floor for what a human is shown: the bar for
        # "send without me looking" should exceed "show me this".
        assert p.auto_apply_min_score >= 80


class TestUserControls:
    def test_kill_switch_stops_everything(self, wired):
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=5)],
            [_job()],
            killed=Row(disabled=True),
        )
        assert aat.sweep_auto_apply()["queued"] == 0

    def test_no_active_resume_means_no_applications(self, wired):
        u = _user(auto_apply_enabled=True, auto_apply_daily_cap=5)
        u.active_resume_id = None
        wired["session"] = FakeSession([u], [_job()])
        assert aat.sweep_auto_apply()["queued"] == 0

    def test_daily_cap_is_respected(self, wired):
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=2)],
            [_job() for _ in range(10)],
        )
        assert aat.sweep_auto_apply()["queued"] == 2
        assert len(wired["enqueued"]) == 2

    def test_cap_already_spent_queues_nothing(self, wired):
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=3)],
            [_job() for _ in range(5)],
            submitted_count=3,
        )
        assert aat.sweep_auto_apply()["queued"] == 0

    def test_never_applies_twice_to_the_same_job(self, wired):
        job = _job()
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=5)],
            [job],
            existing_app_job_ids=[job.id],
        )
        assert aat.sweep_auto_apply()["queued"] == 0

    def test_unparseable_preferences_do_not_wedge_the_sweep(self, wired):
        bad = Row(id=uuid.uuid4(), active_resume_id=uuid.uuid4(),
                  routine_preferences={"auto_apply_enabled": "not-a-bool"})
        good = _user(auto_apply_enabled=True, auto_apply_daily_cap=1)
        wired["session"] = FakeSession([bad, good], [_job()])
        assert aat.sweep_auto_apply()["queued"] == 1


class TestHappyPath:
    def test_creates_an_application_and_enqueues_it(self, wired):
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=1)], [_job()]
        )
        out = aat.sweep_auto_apply()
        assert out == {"users": 1, "queued": 1}
        (app_row,) = wired["session"].added
        assert app_row.status == "prepared"
        assert app_row.apply_method == "api_submit"
        assert app_row.submission_source == "routine"
        assert wired["enqueued"] == [str(app_row.id)]

    def test_session_is_always_closed(self, wired):
        wired["session"] = FakeSession([_user()], [])
        aat.sweep_auto_apply()
        assert wired["session"].closed is True


class TestPlatformScope:
    def test_aggregators_are_never_auto_applied(self):
        assert "linkedin" in aat._NEVER_AUTO_APPLY

    def test_only_auto_submittable_platforms_are_considered(self, wired, monkeypatch):
        """Extraction + submission both required. A platform we can read
        but not drive must not be picked."""
        from app.services import submitters

        monkeypatch.setattr(submitters, "auto_submittable_platforms", lambda: frozenset())
        wired["session"] = FakeSession(
            [_user(auto_apply_enabled=True, auto_apply_daily_cap=5)], [_job()]
        )
        out = aat.sweep_auto_apply()
        assert out["queued"] == 0
        assert "no auto-submittable platforms" in out.get("reason", "")

    def test_lever_is_not_auto_submittable(self):
        """It has extraction but a mandatory hCaptcha, so no submitter."""
        from app.services.submitters import auto_submittable_platforms

        assert "lever" not in auto_submittable_platforms()


class TestFreshness:
    def test_only_recent_postings(self):
        """A stale posting is likely filled; a wasted unattended
        application is worse than a wasted human glance."""
        assert aat._MAX_POSTING_AGE_DAYS <= 30


class TestRegistration:
    def test_task_is_registered(self):
        """F356 — a task absent from tasks/__init__ never runs."""
        from app.workers.celery_app import celery_app

        assert aat.sweep_auto_apply.name in celery_app.tasks

    def test_task_is_beat_scheduled(self):
        from app.workers.celery_app import celery_app

        assert "sweep_auto_apply" in celery_app.conf.beat_schedule
