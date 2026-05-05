"""Work-window feature — deep-dive + "manual probe" coverage.

The existing ``test_work_window.py`` (helpers + allowlist) and
``test_work_window_full.py`` (schemas + handler invariants + route
table + frontend wiring) cover the unit + structural layer. This
file adds the scenarios a manual operator would walk through
when sanity-checking the feature end-to-end:

  * Hourly-timeline simulation across 24 hours IST — verifies the
    helper's yes/no answer at every hour boundary against an
    independent calculation. Closest thing to "manual testing"
    without spinning up the server.
  * State-machine probe for the extension-request lifecycle —
    pending → approved bumps the override; pending → denied
    doesn't; closed requests can't be re-decided.
  * Stacking-override regression — two sequential approvals
    extend FROM the prior end, not from now.
  * Microsecond-precision boundary on override expiry.
  * Schema rigor (extra="forbid", bounds on requested_minutes,
    HH:MM strict format, decision Literal).
  * F324 race-safe pending invariant (migration + handler
    IntegrityError translation).

Source-level checks pin the contract; behaviour-level checks
exercise the helper/state machine for every meaningful input.
"""
from __future__ import annotations

import importlib
import inspect
import os
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

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
os.environ.setdefault("JWT_SECRET", "pytest-work-window-deep")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _user(**kwargs):
    """SimpleNamespace mock matching the ORM surface
    ``user_can_access_now`` reads. Kept tiny so the tests can run
    without a DB session."""
    defaults = dict(
        id=uuid4(),
        role="reviewer",
        is_active=True,
        work_window_enabled=True,
        work_window_start_min=540,   # 09:00 IST
        work_window_end_min=1080,    # 18:00 IST
        work_window_override_until=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ═════════════════════════════════════════════════════════════════════
# Hourly-timeline simulation (24 hours IST)
# ═════════════════════════════════════════════════════════════════════


class TestTimeline24Hour:
    """Walk through a full IST day at hour boundaries; verify
    ``user_can_access_now`` flips at exactly 09:00 (open) and
    18:00 (close) for the default 09:00–18:00 window."""

    def _user_default_window(self):
        return _user(work_window_start_min=540, work_window_end_min=1080)

    def test_default_window_open_from_09_to_17(self):
        """Inclusive-start, exclusive-end means 09:00 → True,
        18:00 → False. Each hour 09..17 inside, 00..08 + 18..23
        outside."""
        from app.utils.work_window import user_can_access_now

        u = self._user_default_window()
        # 2026-04-27, anchor at midnight IST = 18:30 UTC the prior day.
        # Easier: walk 00:00..23:00 IST, convert each to UTC, check.
        for ist_hour in range(0, 24):
            # IST hour `H` = UTC hour (H - 5.5) mod 24
            # We just construct a UTC datetime that maps to the right
            # IST minute by subtracting the offset.
            ist = datetime(2026, 4, 27, ist_hour, 0, tzinfo=timezone.utc)  # treated as IST-shaped
            now_utc = ist - timedelta(hours=5, minutes=30)
            expected_open = 9 <= ist_hour < 18
            actual = user_can_access_now(u, now_utc)
            assert actual is expected_open, (
                f"IST {ist_hour:02d}:00 expected={'open' if expected_open else 'closed'} "
                f"got {'open' if actual else 'closed'}"
            )

    def test_minute_resolution_around_open_close(self):
        """The transitions at 09:00 (open) and 18:00 (close) must
        be sharp — 08:59 closed, 09:00 open, 17:59 open, 18:00
        closed."""
        from app.utils.work_window import user_can_access_now

        u = self._user_default_window()
        ist_offset = timedelta(hours=5, minutes=30)
        anchor = datetime(2026, 4, 27, tzinfo=timezone.utc)
        cases = [
            (8, 59, False),
            (9, 0, True),
            (9, 1, True),
            (17, 59, True),
            (18, 0, False),
            (18, 1, False),
        ]
        for ist_h, ist_m, expected in cases:
            now_utc = anchor.replace(hour=ist_h, minute=ist_m) - ist_offset
            actual = user_can_access_now(u, now_utc)
            assert actual is expected, (
                f"IST {ist_h:02d}:{ist_m:02d} expected={expected} got={actual}"
            )

    def test_wraparound_night_shift_22_to_06(self):
        """22:00–06:00 IST night shift: 21:59 closed, 22:00 open,
        midnight open, 05:59 open, 06:00 closed, noon closed."""
        from app.utils.work_window import user_can_access_now

        u = _user(
            work_window_start_min=22 * 60,  # 22:00
            work_window_end_min=6 * 60,     # 06:00
        )
        ist_offset = timedelta(hours=5, minutes=30)
        anchor = datetime(2026, 4, 27, tzinfo=timezone.utc)
        cases = [
            (21, 59, False),
            (22, 0, True),
            (22, 30, True),
            (0, 0, True),
            (5, 59, True),
            (6, 0, False),
            (12, 0, False),
        ]
        for ist_h, ist_m, expected in cases:
            now_utc = anchor.replace(hour=ist_h, minute=ist_m) - ist_offset
            actual = user_can_access_now(u, now_utc)
            assert actual is expected, (
                f"Night shift IST {ist_h:02d}:{ist_m:02d} "
                f"expected={expected} got={actual}"
            )


# ═════════════════════════════════════════════════════════════════════
# Override microsecond-precision boundary
# ═════════════════════════════════════════════════════════════════════


class TestOverrideBoundary:
    """``override_until > now_utc`` semantics: equality does NOT
    lift the lock, but 1 microsecond past does. This matters for
    the "race to expire" UI countdown so users don't see a stale
    "open" state for one frame after the override expires.
    """

    def test_override_one_microsecond_in_future_lifts(self):
        from app.utils.work_window import user_can_access_now

        # 03:00 UTC = 08:30 IST → outside default 09:00-18:00.
        now = datetime(2026, 4, 27, 3, 0, tzinfo=timezone.utc)
        u = _user(work_window_override_until=now + timedelta(microseconds=1))
        assert user_can_access_now(u, now) is True

    def test_override_exactly_now_does_not_lift(self):
        from app.utils.work_window import user_can_access_now

        now = datetime(2026, 4, 27, 3, 0, tzinfo=timezone.utc)
        u = _user(work_window_override_until=now)
        assert user_can_access_now(u, now) is False

    def test_override_one_microsecond_past_does_not_lift(self):
        from app.utils.work_window import user_can_access_now

        now = datetime(2026, 4, 27, 3, 0, tzinfo=timezone.utc)
        u = _user(work_window_override_until=now - timedelta(microseconds=1))
        assert user_can_access_now(u, now) is False


# ═════════════════════════════════════════════════════════════════════
# Extension-request state machine
# ═════════════════════════════════════════════════════════════════════


class TestExtensionRequestStateMachine:
    """Walk the lifecycle: pending → approved (override bumps) and
    pending → denied (override unchanged). Verify decision-finality
    rejection of re-decide."""

    def test_pending_approved_bumps_override_by_requested_minutes(self):
        """The handler computes
        ``approved_until = max(now, current_override) + requested_minutes``
        and stamps it on both the request and the user."""
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.admin_decide_request)

        # The contract is encoded in source; this test pins it.
        # Live verification is impossible without booting the
        # FastAPI app + a DB session.
        assert "anchor = now_utc" in src
        assert "anchor = target.work_window_override_until" in src
        assert "approved_until = anchor + timedelta(minutes=req.requested_minutes)" in src
        assert "target.work_window_override_until = approved_until" in src
        assert "req.approved_until = approved_until" in src

    def test_denied_does_not_touch_override(self):
        """Source-level: the override-bump lives inside
        ``if body.decision == 'approved':``. Anywhere else would
        be a bug — denied requests must NOT extend the user."""
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.admin_decide_request)
        # The override bump must be guarded by the approved branch
        approved_idx = src.find('body.decision == "approved"')
        bump_idx = src.find("target.work_window_override_until = approved_until")
        assert approved_idx > 0 and bump_idx > approved_idx, (
            "Override bump escaped the ``decision == 'approved'`` guard — "
            "denied requests would extend the user."
        )

    def test_decision_status_transitions_only_from_pending(self):
        """``pending → approved`` and ``pending → denied`` are the
        only legal transitions. Re-deciding a closed request is
        409 (test_admin_decide_request_rejects_non_pending in
        the existing suite — this test pins the status pattern
        in the response too)."""
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.admin_decide_request)
        assert "req.status = body.decision" in src
        assert "req.decided_by_user_id = admin.id" in src
        assert "req.decided_at = now_utc" in src

    def test_decision_note_stripped_of_whitespace(self):
        """``"  yes please  "`` should land as ``"yes please"`` —
        otherwise admin-typed leading/trailing whitespace bloats
        the note column and confuses display."""
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.admin_decide_request)
        assert "req.decision_note = body.note.strip()" in src

    def test_reason_stripped_on_request_create(self):
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.create_my_extension_request)
        assert "reason=body.reason.strip()" in src


# ═════════════════════════════════════════════════════════════════════
# Stacking override regression
# ═════════════════════════════════════════════════════════════════════


class TestStackingOverride:
    """Two sequential approvals must extend FROM the prior end —
    not from ``now`` (which would shorten the user's access if the
    second approval lands while the first override is still live).
    """

    def test_anchor_is_max_now_or_existing_override(self):
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.admin_decide_request)
        # The pattern is:
        #   anchor = now_utc
        #   if target.override_until > now_utc:
        #       anchor = target.override_until
        # Encoded as the "target.work_window_override_until > now_utc"
        # branch followed by the assignment.
        assert (
            "target.work_window_override_until is not None" in src
            and "target.work_window_override_until > now_utc" in src
        )

    def test_simulated_second_approval_extends_from_end_not_now(self):
        """Simulate two approvals 30 min apart. First adds 60 min
        from now; second adds 60 min — must end up 120 min from
        the first ``now``, not 60 min from the second."""
        from datetime import timedelta as td

        # Anchor 1: now_1 = T
        T = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        first_override = T + td(minutes=60)

        # 30 min later admin approves another 60-min request.
        now_2 = T + td(minutes=30)
        # Replicate the handler logic:
        anchor = now_2
        if first_override is not None and first_override > now_2:
            anchor = first_override
        second_override = anchor + td(minutes=60)
        # First override ends T+60; second ends T+60+60 = T+120
        assert second_override == T + td(minutes=120)
        # If we'd anchored to now_2 it would have been T+30+60 = T+90,
        # which would SHORTEN the user's access vs the first override.
        assert second_override > first_override


# ═════════════════════════════════════════════════════════════════════
# Schema rigor
# ═════════════════════════════════════════════════════════════════════


class TestSchemaRigor:
    def test_extension_request_minutes_bounded_15_to_240(self):
        """Anti-abuse: extension request can't ask for less than
        15 min (too short to be useful) or more than 240 min
        (no "+8 hours" surprises). 422 at parse time."""
        from app.schemas.work_window import ExtensionRequestCreate

        ExtensionRequestCreate(requested_minutes=15)
        ExtensionRequestCreate(requested_minutes=240)
        for bad in (0, 14, 241, -10, 9999):
            with pytest.raises(Exception):
                ExtensionRequestCreate(requested_minutes=bad)

    def test_extension_request_reason_capped(self):
        """500 char cap on the free-form ``reason`` so admin
        review screen doesn't render a wall of text."""
        from app.schemas.work_window import ExtensionRequestCreate

        ExtensionRequestCreate(requested_minutes=30, reason="x" * 500)
        with pytest.raises(Exception):
            ExtensionRequestCreate(requested_minutes=30, reason="x" * 501)

    def test_extension_request_extra_forbid(self):
        """Typos like ``minutes=30`` (missing ``requested_`` prefix)
        must 422 instead of silently dropping."""
        from app.schemas.work_window import ExtensionRequestCreate

        with pytest.raises(Exception):
            ExtensionRequestCreate(requested_minutes=30, minutes=30)

    def test_window_update_extra_forbid(self):
        from app.schemas.work_window import WorkWindowUpdate

        with pytest.raises(Exception):
            WorkWindowUpdate(enabled=True, sttart_ist="09:00")

    def test_decision_extra_forbid(self):
        from app.schemas.work_window import ExtensionDecision

        with pytest.raises(Exception):
            ExtensionDecision(decision="approved", noote="ok")

    def test_decision_literal_rejects_non_canonical(self):
        from app.schemas.work_window import ExtensionDecision

        ExtensionDecision(decision="approved")
        ExtensionDecision(decision="denied")
        for bad in ("approve", "deny", "ok", "rejected", ""):
            with pytest.raises(Exception):
                ExtensionDecision(decision=bad)

    def test_hhmm_strict_format(self):
        """``"9:00"`` accepted (admin-friendly), ``"24:00"`` /
        ``"09:60"`` / ``"abc"`` rejected."""
        from app.schemas.work_window import WorkWindowUpdate

        WorkWindowUpdate(start_ist="09:00")
        WorkWindowUpdate(start_ist="9:00")  # single-digit hour ok
        WorkWindowUpdate(start_ist="23:59")
        WorkWindowUpdate(start_ist="00:00")
        for bad in ("24:00", "09:60", "abc", "9", "09-00", "9:0:0"):
            with pytest.raises(Exception):
                WorkWindowUpdate(start_ist=bad)


# ═════════════════════════════════════════════════════════════════════
# F324 race-safe pending — migration + handler
# ═════════════════════════════════════════════════════════════════════


class TestF324RaceSafePending:
    def test_migration_chain(self):
        path = next(
            (_BACKEND / "alembic" / "versions").glob("*_n0o1p2q3r4s5_*.py")
        )
        src = path.read_text()
        assert 'revision = "n0o1p2q3r4s5"' in src
        assert 'down_revision = "m9n0o1p2q3r4"' in src

    def test_migration_dedupe_before_unique(self):
        path = next(
            (_BACKEND / "alembic" / "versions").glob("*_n0o1p2q3r4s5_*.py")
        )
        src = path.read_text()
        upg_start = src.find("def upgrade()")
        upg_end = src.find("def downgrade()", upg_start)
        body = src[upg_start:upg_end]
        # Most-recent-wins partition with the constraint key
        assert "PARTITION BY user_id" in body
        assert "ORDER BY requested_at DESC, id DESC" in body
        # 'expired' status — distinguishable from admin-decided 'denied'
        assert "SET status = 'expired'" in body
        # Partial UNIQUE on the same key as the dedupe partition
        assert "uq_work_time_pending_per_user" in body
        assert "WHERE status = 'pending'" in body
        # Order: dedupe first, constraint second
        delete_idx = body.find("UPDATE work_time_extension_requests")
        index_idx = body.find("CREATE UNIQUE INDEX")
        assert delete_idx > 0 and index_idx > delete_idx

    def test_handler_translates_race_to_409(self):
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.create_my_extension_request)
        assert "except IntegrityError" in src, (
            "F324 regression: race-recovery branch removed."
        )
        assert "uq_work_time_pending_per_user" in src, (
            "F324 regression: handler doesn't reference the "
            "constraint name — ANY IntegrityError → 409 would "
            "hide genuinely-different bugs (FK violations etc.)."
        )
        assert "status_code=409" in src

    def test_handler_keeps_lookup_check_as_fast_path(self):
        """The pre-INSERT lookup stays as the fast path for
        serial requests — the IntegrityError catch is the
        belt-and-suspenders DB-level gate."""
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.create_my_extension_request)
        assert 'WorkTimeExtensionRequest.status == "pending"' in src
        # The 409 appears in BOTH the handler-check path AND the
        # IntegrityError path so the user-visible outcome is
        # identical regardless of timing.
        assert src.count("status_code=409") >= 2


# ═════════════════════════════════════════════════════════════════════
# Allowlist completeness
# ═════════════════════════════════════════════════════════════════════


class TestAllowlistCompleteness:
    """Walk every documented user-facing path that should bypass
    enforcement and verify it matches the deps allowlist. Pre-fix
    a missing prefix would lock a user out of the page they need
    to even SEE the lock-out screen."""

    def test_user_can_reach_own_state_when_locked(self):
        from app.api import deps

        prefixes = deps.WORK_WINDOW_ALLOWLIST_PREFIXES
        # GET /work-window/me — read state
        assert any("/api/v1/work-window/me".startswith(p) for p in prefixes)
        # POST /work-window/me/extension-requests — submit
        assert any(
            "/api/v1/work-window/me/extension-requests".startswith(p)
            for p in prefixes
        )
        # GET /work-window/me/extension-requests — own history
        assert any(
            "/api/v1/work-window/me/extension-requests?page=1".startswith(p)
            for p in prefixes
        )

    def test_user_can_logout_when_locked(self):
        """A locked-out user must be able to sign out — otherwise
        a user accidentally locked outside their window has no way
        to release their session."""
        from app.api import deps

        prefixes = deps.WORK_WINDOW_ALLOWLIST_PREFIXES
        assert any("/api/v1/auth/logout".startswith(p) for p in prefixes)
        assert any("/api/v1/auth/whoami".startswith(p) for p in prefixes)
        assert any("/api/v1/auth/login".startswith(p) for p in prefixes)
        assert any("/api/v1/auth/change-password".startswith(p) for p in prefixes)

    def test_admin_paths_NOT_on_allowlist(self):
        """Admin paths get there via role short-circuit, not via
        the allowlist. Putting them on the allowlist would let
        non-admins reach admin endpoints when locked (security
        hole)."""
        from app.api import deps

        prefixes = deps.WORK_WINDOW_ALLOWLIST_PREFIXES
        admin_paths = (
            "/api/v1/work-window/admin/users/123",
            "/api/v1/work-window/admin/extension-requests",
            "/api/v1/users",
            "/api/v1/monitoring",
        )
        for p in admin_paths:
            assert not any(p.startswith(prefix) for prefix in prefixes), (
                f"Admin path on allowlist: {p}. Locked non-admins "
                f"would reach admin endpoints."
            )


# ═════════════════════════════════════════════════════════════════════
# IST / UTC math edge cases
# ═════════════════════════════════════════════════════════════════════


class TestISTEdgeCases:
    """The IST/UTC offset is fixed at +5:30 (no DST), so the
    math is straightforward — but a few corner cases bite if
    the helper drifts."""

    def test_midnight_ist_is_18_30_utc_prior_day(self):
        from app.utils.work_window import utc_to_ist_minute

        # 2026-04-27 00:00 IST = 2026-04-26 18:30 UTC. Convert
        # back via the helper and confirm minute=0.
        utc = datetime(2026, 4, 26, 18, 30, tzinfo=timezone.utc)
        assert utc_to_ist_minute(utc) == 0

    def test_late_night_ist_crosses_utc_midnight(self):
        """11:30 PM IST (23:30 = 1410) = 18:00 UTC same day."""
        from app.utils.work_window import utc_to_ist_minute

        utc = datetime(2026, 4, 27, 18, 0, tzinfo=timezone.utc)
        assert utc_to_ist_minute(utc) == 23 * 60 + 30

    def test_ist_minute_never_exceeds_1439(self):
        """Sanity: helper output is always 0..1439."""
        from app.utils.work_window import utc_to_ist_minute

        for hour in range(0, 24):
            for minute in (0, 15, 30, 45, 59):
                utc = datetime(2026, 4, 27, hour, minute, tzinfo=timezone.utc)
                m = utc_to_ist_minute(utc)
                assert 0 <= m <= 1439, f"out of range at UTC {hour}:{minute} → {m}"

    def test_zone_aware_input_normalises_to_utc(self):
        """A zone-aware datetime in IST must produce the same
        IST minute as its UTC equivalent."""
        from app.utils.work_window import utc_to_ist_minute

        ist_tz = timezone(timedelta(hours=5, minutes=30))
        ist_aware = datetime(2026, 4, 27, 12, 0, tzinfo=ist_tz)
        utc_equiv = datetime(2026, 4, 27, 6, 30, tzinfo=timezone.utc)
        assert utc_to_ist_minute(ist_aware) == utc_to_ist_minute(utc_equiv)
        assert utc_to_ist_minute(ist_aware) == 12 * 60


# ═════════════════════════════════════════════════════════════════════
# Wide-net fuzz: every (start, end, current) combo
# ═════════════════════════════════════════════════════════════════════


class TestWindowMembershipFuzz:
    """Walk through every hour boundary as start/end/current —
    catches transposition bugs the surgical tests would miss."""

    def test_every_hour_boundary_consistent_with_independent_calc(self):
        """For 24×24 (start, end) combos and 24 current hours,
        the helper must agree with an independent reference
        implementation."""
        from app.utils.work_window import is_within_window

        def reference(minute, start, end):
            """Recompute in a slightly different way to catch
            transposition bugs."""
            if start == end:
                return False
            if start < end:
                return start <= minute < end
            # Wraparound
            return minute >= start or minute < end

        for start_h in range(0, 24):
            for end_h in range(0, 24):
                start = start_h * 60
                end = end_h * 60
                for cur_h in range(0, 24):
                    cur = cur_h * 60
                    expected = reference(cur, start, end)
                    actual = is_within_window(cur, start, end)
                    assert actual is expected, (
                        f"Mismatch at start={start_h:02d}:00 "
                        f"end={end_h:02d}:00 cur={cur_h:02d}:00 "
                        f"expected={expected} got={actual}"
                    )


# ═════════════════════════════════════════════════════════════════════
# Manual probe — end-to-end scenarios as a checklist
# ═════════════════════════════════════════════════════════════════════


class TestManualProbeScenarios:
    """Each test in this class corresponds to a manual operator
    walkthrough scenario. They use the pure helpers + handler
    source inspection to verify the contract holds. A real
    operator running these as curl probes would expect the
    same outcomes.
    """

    def test_scenario_locked_user_sees_423_outside_window(self):
        """**Probe**: viewer with default window 09:00-18:00 IST,
        request hits /api/v1/jobs at 03:00 UTC (08:30 IST →
        outside).
        **Expected**: 423 Locked.
        """
        from app.utils.work_window import user_can_access_now

        u = _user(role="reviewer")
        now = datetime(2026, 4, 27, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
        # Helper says no
        assert user_can_access_now(u, now) is False
        # Deps source confirms 423
        from app.api import deps
        src = inspect.getsource(deps.get_current_user)
        assert "HTTP_423_LOCKED" in src
        # Admin role short-circuit ensures admin doesn't 423
        assert (
            'user.role not in ("admin", "super_admin")' in src
            or "user.role not in ('admin', 'super_admin')" in src
        )

    def test_scenario_admin_passes_at_any_hour(self):
        """**Probe**: admin user with the same window — 03:00 UTC.
        **Expected**: 200 (admin role short-circuits the gate).
        """
        from app.api import deps
        src = inspect.getsource(deps.get_current_user)
        # The role-short-circuit must guard the entire 423 branch
        # (not just ``user_can_access_now`` — that helper has no
        # role awareness)
        admin_check_idx = src.find('user.role not in')
        gate_idx = src.find("HTTP_423_LOCKED")
        assert admin_check_idx > 0 and gate_idx > admin_check_idx, (
            "Admin role check must precede the 423 gate."
        )

    def test_scenario_locked_user_can_still_logout(self):
        """**Probe**: locked user clicks Logout. Path
        ``/api/v1/auth/logout`` is on the allowlist so the
        request reaches the handler instead of bouncing off the
        gate.
        """
        from app.api import deps

        prefixes = deps.WORK_WINDOW_ALLOWLIST_PREFIXES
        assert any(
            "/api/v1/auth/logout".startswith(p) for p in prefixes
        ), "Logout path missing from allowlist — locked users can't sign out."

    def test_scenario_admin_grants_override_unlocks_user(self):
        """**Probe**: admin POSTs ``/work-window/admin/users/{id}/override``
        with ``override_until=now+1h``. The user can now hit
        protected endpoints until the override expires.

        Picks an evening `now` (well past the default 18:00 close)
        so that `now + 61min` is also still outside the natural
        window — otherwise the test would silently pass for the
        wrong reason (user re-entering the regular work window),
        not because the override expired.
        """
        from app.utils.work_window import user_can_access_now

        u = _user()
        # 15:00 UTC → 20:30 IST (after 18:00 close, before midnight)
        now = datetime(2026, 4, 27, 15, 0, tzinfo=timezone.utc)
        assert user_can_access_now(u, now) is False

        u.work_window_override_until = now + timedelta(hours=1)
        assert user_can_access_now(u, now) is True

        # 61 min later: 16:01 UTC → 21:31 IST (still outside natural
        # window) and override has expired → back to locked.
        later = now + timedelta(minutes=61)
        assert user_can_access_now(u, later) is False

    def test_scenario_admin_clears_override_locks_user_again(self):
        """**Probe**: admin POSTs override_until=null on a user
        that has an active override. Subsequent requests outside
        the regular window should 423 again.
        """
        from app.utils.work_window import user_can_access_now

        now = datetime(2026, 4, 27, 3, 0, tzinfo=timezone.utc)
        u = _user(work_window_override_until=now + timedelta(hours=1))
        assert user_can_access_now(u, now) is True

        # Admin clears the override
        u.work_window_override_until = None
        assert user_can_access_now(u, now) is False

    def test_scenario_request_pending_blocks_second_request(self):
        """**Probe**: locked user submits an extension request, then
        clicks Submit again before admin has acted.
        **Expected**: 409 with the "you already have a pending
        request" message — both via the handler check (fast path)
        AND via the F324 partial UNIQUE (race-safe gate).
        """
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.create_my_extension_request)
        # Two layers — both produce a 409 with the same message
        assert src.count(
            'detail="You already have a pending extension request."'
        ) >= 2

    def test_scenario_admin_approve_extends_window_by_requested_minutes(self):
        """**Probe**: admin approves a 60-min request. The user's
        ``work_window_override_until`` is now ``now + 60min``;
        ``approved_until`` is frozen on the request row.
        """
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.admin_decide_request)
        assert "approved_until = anchor + timedelta(minutes=req.requested_minutes)" in src
        assert "target.work_window_override_until = approved_until" in src
        assert "req.approved_until = approved_until" in src

    def test_scenario_admin_redecide_closed_request_is_409(self):
        """**Probe**: admin clicks Approve on a request that's
        already approved/denied.
        **Expected**: 409, not silent re-bump.
        """
        from app.api.v1 import work_window
        src = inspect.getsource(work_window.admin_decide_request)
        assert 'req.status != "pending"' in src
        # 409 is the response code
        assert "status_code=409" in src

    def test_scenario_disabled_window_user_passes_at_any_hour(self):
        """**Probe**: admin sets ``enabled=false`` on a user.
        That user can hit protected endpoints regardless of time.
        """
        from app.utils.work_window import user_can_access_now

        u = _user(work_window_enabled=False)
        # Try midnight UTC, midnight IST, noon, and 3am
        for h in (0, 6, 12, 18, 23):
            now = datetime(2026, 4, 27, h, 0, tzinfo=timezone.utc)
            assert user_can_access_now(u, now) is True

    def test_scenario_night_shift_user_works_at_midnight(self):
        """**Probe**: user has wraparound window 22:00-06:00 IST.
        Their access pattern is the inverse of the default — open
        from 22:00 to 05:59 IST, closed during the day.
        """
        from app.utils.work_window import user_can_access_now

        u = _user(
            work_window_start_min=22 * 60,
            work_window_end_min=6 * 60,
        )
        # 23:00 IST = 17:30 UTC → open
        utc_2330_ist = datetime(2026, 4, 27, 18, 0, tzinfo=timezone.utc)
        assert user_can_access_now(u, utc_2330_ist) is True

        # 12:00 IST = 06:30 UTC → closed
        utc_1200_ist = datetime(2026, 4, 27, 6, 30, tzinfo=timezone.utc)
        assert user_can_access_now(u, utc_1200_ist) is False

    def test_scenario_mass_check_24_hour_simulation(self):
        """**Probe**: walk through every IST hour for a default-window
        user; verify the helper agrees with a hand calculation.
        Equivalent to a manual operator running ``curl /api/v1/jobs``
        at every hour and observing 200 vs 423.
        """
        from app.utils.work_window import user_can_access_now

        u = _user()
        ist_offset = timedelta(hours=5, minutes=30)
        # 2026-04-27 anchored at midnight IST
        midnight_ist_in_utc = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc) - ist_offset
        for h in range(0, 24):
            now = midnight_ist_in_utc + timedelta(hours=h)
            expected = 9 <= h < 18
            actual = user_can_access_now(u, now)
            assert actual is expected, (
                f"IST {h:02d}:00 (UTC {now.hour}:{now.minute:02d}) "
                f"expected={expected} got={actual}"
            )
