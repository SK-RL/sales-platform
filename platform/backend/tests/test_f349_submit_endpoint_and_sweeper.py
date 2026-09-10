"""F349 — making server-side apply reachable, and un-stranding it.

F347 built the task but nothing enqueued it, so the feature was dead
code. This adds ``POST /applications/{id}/submit`` and the sweeper that
rescues rows abandoned mid-submit.

The two rules pinned here are both about not applying twice:
  * the endpoint refuses to re-submit anything already sent
  * the sweeper moves stranded rows to ``failed``, never straight back
    into a retry — we don't know whether the dead worker died before or
    after the submit click.
"""

import pytest
from app.api.v1.applications import (
    SubmitApplicationRequest,
    VALID_TRANSITIONS,
)
from app.main import app as fastapi_app
from app.workers.celery_app import celery_app
from app.workers.tasks import submit_application_task, sweep_stuck_in_flight
from app.workers.tasks.apply_task import (
    STATUS_FAILED,
    STATUS_IN_FLIGHT,
    STATUS_NEEDS_USER,
    STUCK_IN_FLIGHT_MINUTES,
)


class TestRouteIsWired:
    def test_submit_route_exists(self):
        paths = {getattr(r, "path", "") for r in fastapi_app.routes}
        assert "/api/v1/applications/{app_id}/submit" in paths

    def test_route_is_post_only(self):
        route = next(
            r for r in fastapi_app.routes
            if getattr(r, "path", "") == "/api/v1/applications/{app_id}/submit"
        )
        assert route.methods == {"POST"}


class TestRequestSchema:
    def test_dry_run_defaults_to_false(self):
        """A bare POST submits for real, matching every other action
        endpoint. Dry run is opt-in, not the default."""
        assert SubmitApplicationRequest().dry_run is False

    def test_dry_run_can_be_set(self):
        assert SubmitApplicationRequest(dry_run=True).dry_run is True

    def test_unknown_keys_are_rejected(self):
        """F306 sweep: a typo like `dryRun` must 422, not be ignored."""
        with pytest.raises(Exception):
            SubmitApplicationRequest(dryRun=True)


class TestTaskRegistration:
    def test_submit_task_is_registered(self):
        """F356: a task absent from tasks/__init__ never registers and
        the worker rejects every firing."""
        assert submit_application_task.name in celery_app.tasks

    def test_sweeper_is_registered(self):
        assert sweep_stuck_in_flight.name in celery_app.tasks

    def test_sweeper_is_beat_scheduled(self):
        sched = celery_app.conf.beat_schedule
        assert "sweep_stuck_in_flight" in sched
        assert sched["sweep_stuck_in_flight"]["task"] == sweep_stuck_in_flight.name

    def test_submit_task_is_not_beat_scheduled(self):
        """It's user-triggered. A beat entry would submit applications
        on a timer, which is not a thing we ever want."""
        tasks = {v["task"] for v in celery_app.conf.beat_schedule.values()}
        assert submit_application_task.name not in tasks


class TestSweeperSemantics:
    def test_window_is_generous(self):
        """Slow uploads are legitimate; the sweeper targets dead rows,
        not slow ones."""
        assert STUCK_IN_FLIGHT_MINUTES >= 15

    def test_swept_rows_land_in_failed_not_needs_user(self):
        """`failed` is retryable by the user. We deliberately do NOT
        auto-retry: if the worker died after the submit click the
        employer may already have the application."""
        assert STATUS_FAILED in VALID_TRANSITIONS[STATUS_IN_FLIGHT]
        assert "in_flight" in VALID_TRANSITIONS[STATUS_FAILED]

    def test_failed_can_also_be_closed_by_hand(self):
        assert "applied" in VALID_TRANSITIONS[STATUS_FAILED]

    def test_needs_user_is_a_distinct_outcome(self):
        """Gate refusals are not failures — retrying changes nothing
        until a person acts, so they must not share a state."""
        assert STATUS_NEEDS_USER != STATUS_FAILED
        assert STATUS_NEEDS_USER in VALID_TRANSITIONS[STATUS_IN_FLIGHT]


class TestResubmitGuard:
    """The endpoint refuses states where sending again would duplicate a
    real application at the employer. Pinned as data so the guard can't
    be loosened without this failing."""

    ALREADY_SENT = ("submitted", "applied", "interview", "offer")

    def test_sent_states_have_no_path_back_to_in_flight(self):
        for state in self.ALREADY_SENT:
            assert "in_flight" not in VALID_TRANSITIONS.get(state, []), state

    def test_pre_send_states_can_reach_in_flight(self):
        for state in ("prepared", "needs_user", "failed"):
            assert "in_flight" in VALID_TRANSITIONS[state], state
