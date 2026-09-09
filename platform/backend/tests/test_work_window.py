"""Unit tests for the work-time window helpers + deps allowlist.

The enforcement layer hangs off three small invariants — if any of
these regress, every protected endpoint either over-blocks or
silently lets locked-out users through. The tests here are
deliberately tight so future refactors get fast feedback:

  1. IST conversion is a fixed UTC+5:30 offset.
  2. Wraparound windows (start > end) are accepted.
  3. The deps-level allowlist matches by path prefix exactly — no
     fuzzy substring match that would let ``/api/v1/foo/auth/bar``
     leak through.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api import deps
from app.utils.work_window import (
    format_minute_ist,
    is_within_window,
    parse_hhmm_to_minute,
    user_can_access_now,
    utc_to_ist_minute,
)


# ─── helpers ─────────────────────────────────────────────────────


def _user(**kwargs):
    """Build a SimpleNamespace shaped enough for the helpers under test.

    Avoids hitting the SQLAlchemy session — these are pure-function
    tests. The User model surface used by ``user_can_access_now`` is
    just five attributes; mirror them here.
    """
    defaults = dict(
        work_window_enabled=True,
        work_window_start_min=540,
        work_window_end_min=1080,
        work_window_override_until=None,
        role="reviewer",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ─── conversions ─────────────────────────────────────────────────


def test_utc_to_ist_minute_known_anchor():
    """2026-04-27 00:00 UTC = 05:30 IST = minute 330."""
    assert utc_to_ist_minute(datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)) == 330


def test_utc_to_ist_minute_naive_treated_as_utc():
    """Naive datetimes are treated as UTC (matches codebase convention)."""
    naive = datetime(2026, 4, 27, 12, 0)
    assert utc_to_ist_minute(naive) == 17 * 60 + 30  # 17:30 IST


def test_format_and_parse_round_trip():
    for s in ("00:00", "09:00", "09:30", "18:00", "23:59"):
        assert format_minute_ist(parse_hhmm_to_minute(s)) == s


def test_parse_hhmm_rejects_invalid():
    # Single-digit hour ("9:00") is accepted on purpose — matches what
    # admins type. Out-of-range values, non-numeric input, and missing
    # separator still 422.
    for bad in ("24:00", "09:60", "abc", "9", "09-00"):
        with pytest.raises(ValueError):
            parse_hhmm_to_minute(bad)


# ─── window membership ───────────────────────────────────────────


def test_normal_window_inclusive_start_exclusive_end():
    # 09:00–18:00
    assert is_within_window(540, 540, 1080) is True   # exact start
    assert is_within_window(720, 540, 1080) is True   # noon
    assert is_within_window(1079, 540, 1080) is True  # one min before close
    assert is_within_window(1080, 540, 1080) is False  # exact end excluded
    assert is_within_window(539, 540, 1080) is False   # one min before open


def test_wraparound_night_shift():
    # 22:00–06:00
    start, end = 22 * 60, 6 * 60
    assert is_within_window(22 * 60, start, end) is True       # 22:00
    assert is_within_window(0, start, end) is True             # midnight
    assert is_within_window(5 * 60 + 59, start, end) is True   # 05:59
    assert is_within_window(6 * 60, start, end) is False       # exact end
    assert is_within_window(12 * 60, start, end) is False      # noon


def test_zero_length_window_always_closed():
    """Misconfigured start==end shouldn't accidentally allow 24/7."""
    assert is_within_window(0, 540, 540) is False
    assert is_within_window(540, 540, 540) is False


# ─── user_can_access_now escape hatches ──────────────────────────


def test_disabled_window_always_passes():
    u = _user(work_window_enabled=False)
    # 03:00 UTC = 08:30 IST, before the 09:00 default — but the gate
    # is off, so access is allowed.
    now = datetime(2026, 4, 27, 3, 0, tzinfo=timezone.utc)
    assert user_can_access_now(u, now) is True


def test_active_override_lifts_the_lock():
    now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)  # 05:30 IST
    # Default 09:00–18:00 IST window — would normally block.
    blocked = _user()
    assert user_can_access_now(blocked, now) is False
    # Same user with an override valid for another hour passes.
    extended = _user(work_window_override_until=now + timedelta(hours=1))
    assert user_can_access_now(extended, now) is True


def test_expired_override_does_not_lift():
    now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    expired = _user(work_window_override_until=now - timedelta(minutes=1))
    assert user_can_access_now(expired, now) is False


# ─── allowlist semantics ─────────────────────────────────────────


def test_allowlist_prefixes_match_only_prefixes():
    """``/api/v1/auth/login`` allowed; ``/api/v1/foo/auth/login`` is not."""
    prefixes = deps.WORK_WINDOW_ALLOWLIST_PREFIXES
    # Sanity — the allowlist is the documented set, not a regression
    # of "everything is bypassed". Two prefixes total.
    assert len(prefixes) == 2
    allowed_paths = (
        "/api/v1/auth/login",
        "/api/v1/auth/whoami",
        "/api/v1/work-window/me",
        "/api/v1/work-window/me/extension-requests",
    )
    for p in allowed_paths:
        assert any(p.startswith(prefix) for prefix in prefixes), p

    blocked_paths = (
        "/api/v1/jobs",
        "/api/v1/work-window/admin/users/123",  # admin route — gated by role, not allowlist
        "/api/v1/foo/auth/login",                # would-be substring leak
    )
    for p in blocked_paths:
        assert not any(p.startswith(prefix) for prefix in prefixes), p


def test_router_registered_in_v1_router():
    """Smoke: ``/work-window/me`` is reachable in the v1 router."""
    from tests._routes import registered_paths

    paths = registered_paths()
    assert "/api/v1/work-window/me" in paths
    assert "/api/v1/work-window/admin/users/{user_id}" in paths
    assert "/api/v1/work-window/admin/extension-requests" in paths
