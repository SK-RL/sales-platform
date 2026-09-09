"""Comprehensive coverage for the work-time window feature.

The unit-level invariants of the IST math + allowlist live in
``test_work_window.py``. This file extends coverage to the surfaces
that historically broke on this codebase:

  * Pydantic schemas — ``extra="forbid"``, enum membership, range
    validators, the HH:MM field validator.
  * SQLAlchemy column shapes — types, nullability, defaults.
    Migration vs model drift here would silently break enforcement
    on every pod that re-pulls from the DB.
  * Handler source guards — the assertions are about the right
    checks being present in code (anti-spam 409, override-stacking,
    pending-only decision). Mirrors the pattern in
    ``test_force_change_password.py`` — pytest-fast, regression-tight.
  * Route table — every endpoint wired with the right HTTP method on
    the documented path. A typo on ``GET`` vs ``PATCH`` would
    silently move a route into "method not allowed" and the
    frontend would render an error banner.
  * Behavioural edges on ``user_can_access_now`` — naive timestamps
    in the override column (legacy data), exactly-equal boundary,
    enabled+override+outside-window stacking.

The goal is fast feedback when something regresses — every test
runs in under a millisecond and the file imports nothing live (no
DB, no Redis, no Celery).
"""

from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
os.environ.setdefault("JWT_SECRET", "pytest-work-time-windows")


# ═════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ═════════════════════════════════════════════════════════════════════


class TestSchemas:
    """Lock the wire contract — bad input must 422 at the schema layer
    so the router never sees ``"09:60"`` or ``requested_minutes=10000``.
    """

    def test_work_window_update_rejects_unknown_fields(self):
        """``extra="forbid"`` keeps the schema authoritative — a
        misspelled field name like ``start_minute_ist`` 422s instead
        of being silently dropped (which would let the admin think
        their PATCH worked while no column actually changed)."""
        from pydantic import ValidationError

        from app.schemas.work_window import WorkWindowUpdate

        # Sanity — the documented fields all parse cleanly.
        WorkWindowUpdate(enabled=True, start_ist="09:00", end_ist="18:00")

        with pytest.raises(ValidationError):
            WorkWindowUpdate(start_minute_ist=540)  # type: ignore[call-arg]

    def test_work_window_update_validates_hhmm_via_field_validator(self):
        """The ``HH:MM`` validator must run on both ``start_ist`` and
        ``end_ist``. A regression that drops the validator would let
        ``"09:60"`` through to ``parse_hhmm_to_minute`` in the router,
        which would then 500 instead of 422.
        """
        from pydantic import ValidationError

        from app.schemas.work_window import WorkWindowUpdate

        for bad_field, bad_value in (
            ("start_ist", "09:60"),
            ("end_ist", "24:00"),
            ("start_ist", "abc"),
            ("end_ist", ""),
        ):
            with pytest.raises(ValidationError):
                WorkWindowUpdate(**{bad_field: bad_value})

    def test_work_window_update_partial_patch(self):
        """All fields optional — partial update should parse without
        the absent fields being surfaced as ``None``. The router
        relies on the "not in fields_set" distinction to skip a
        column rather than overwrite with None.
        """
        from app.schemas.work_window import WorkWindowUpdate

        body = WorkWindowUpdate(start_ist="10:30")
        assert body.start_ist == "10:30"
        assert body.end_ist is None
        assert body.enabled is None
        # Confirm dump shape so the handler logic that does
        # ``body.start_ist is not None`` works as documented.
        assert body.model_dump(exclude_unset=True) == {"start_ist": "10:30"}

    def test_extension_request_create_bounds(self):
        """15..240 inclusive — single source of truth for "how long
        can a request be". Out-of-range values 422 at the schema layer
        instead of writing junk to the DB."""
        from pydantic import ValidationError

        from app.schemas.work_window import ExtensionRequestCreate

        # Boundary values pass.
        ExtensionRequestCreate(requested_minutes=15)
        ExtensionRequestCreate(requested_minutes=240)
        # Just outside fail.
        for bad in (0, 14, 241, 1000, -5):
            with pytest.raises(ValidationError):
                ExtensionRequestCreate(requested_minutes=bad)

    def test_extension_request_create_reason_capped(self):
        from pydantic import ValidationError

        from app.schemas.work_window import ExtensionRequestCreate

        # Empty allowed.
        ExtensionRequestCreate(requested_minutes=30, reason="")
        # 500-char allowed.
        ExtensionRequestCreate(requested_minutes=30, reason="x" * 500)
        # 501 rejected — caps prevent unbounded log/storage growth.
        with pytest.raises(ValidationError):
            ExtensionRequestCreate(requested_minutes=30, reason="x" * 501)

    def test_extension_decision_enum(self):
        """Only the two documented decisions parse — a typo like
        ``"approve"`` or ``"reject"`` would otherwise write junk
        ``status`` values to the DB."""
        from pydantic import ValidationError

        from app.schemas.work_window import ExtensionDecision

        ExtensionDecision(decision="approved")
        ExtensionDecision(decision="denied")
        for bad in ("approve", "reject", "pending", "yes", ""):
            with pytest.raises(ValidationError):
                ExtensionDecision(decision=bad)  # type: ignore[arg-type]

    def test_work_window_response_round_trip(self):
        """The ``to_response`` helper must produce a payload that
        re-parses cleanly. Catches a future drift where someone adds
        a required field to the response model without updating the
        helper."""
        from app.schemas.work_window import WorkWindowResponse, to_response

        now = datetime.now(timezone.utc)
        resp = to_response(
            enabled=True,
            start_min=540,
            end_min=1080,
            override_until=None,
            within_window_now=True,
            server_now_utc=now,
        )
        # Round-trip through dict and back — fastest sanity check
        # that every field is set and the right type.
        round_tripped = WorkWindowResponse.model_validate(resp.model_dump())
        assert round_tripped.start_ist == "09:00"
        assert round_tripped.end_ist == "18:00"
        assert round_tripped.enabled is True
        assert round_tripped.within_window_now is True


# ═════════════════════════════════════════════════════════════════════
# SQLAlchemy column shapes
# ═════════════════════════════════════════════════════════════════════


class TestUserModelColumns:
    """Pin every new column on ``users`` to the type/nullability/default
    that the migration creates. A drift here is silent until the first
    write: column nullable on the model + NOT NULL in DB → IntegrityError
    at runtime, not at boot.
    """

    def _col(self, name: str):
        from app.models.user import User

        return User.__table__.c[name]

    def test_work_window_enabled_is_not_null_boolean_default_false(self):
        from sqlalchemy import Boolean

        col = self._col("work_window_enabled")
        assert isinstance(col.type, Boolean)
        assert col.nullable is False, (
            "work_window_enabled must be NOT NULL — a NULL value would "
            "make the enforcement check ambiguous (None != False), "
            "letting locked users slip through if the user_can_access_now "
            "guard is later relaxed to truthy semantics."
        )
        # server_default writes the literal "false" string at DDL time.
        assert "false" in str(col.server_default.arg).lower()

    def test_work_window_start_min_is_not_null_integer_default_540(self):
        from sqlalchemy import Integer

        col = self._col("work_window_start_min")
        assert isinstance(col.type, Integer)
        assert col.nullable is False
        assert "540" in str(col.server_default.arg)

    def test_work_window_end_min_is_not_null_integer_default_1080(self):
        from sqlalchemy import Integer

        col = self._col("work_window_end_min")
        assert isinstance(col.type, Integer)
        assert col.nullable is False
        assert "1080" in str(col.server_default.arg)

    def test_work_window_override_until_is_nullable_tz_aware_datetime(self):
        from sqlalchemy import DateTime

        col = self._col("work_window_override_until")
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True, (
            "override_until must be TIMESTAMPTZ — comparing a naive "
            "DB value against a tz-aware now() either crashes or "
            "silently produces wrong results when the server isn't UTC."
        )
        assert col.nullable is True


class TestExtensionRequestModelColumns:
    """Pin the schema for ``work_time_extension_requests``."""

    def _col(self, name: str):
        from app.models.work_time import WorkTimeExtensionRequest

        return WorkTimeExtensionRequest.__table__.c[name]

    def test_status_default_pending(self):
        col = self._col("status")
        assert col.nullable is False
        assert "pending" in str(col.server_default.arg)

    def test_requested_minutes_not_null(self):
        from sqlalchemy import Integer

        col = self._col("requested_minutes")
        assert isinstance(col.type, Integer)
        assert col.nullable is False

    def test_decided_by_user_id_set_null_on_user_delete(self):
        """If the deciding admin is later deleted we want history
        preserved, not cascade-deleted. ``ON DELETE SET NULL`` keeps
        the row + decision around with a null reviewer.
        """
        from app.models.work_time import WorkTimeExtensionRequest

        col = WorkTimeExtensionRequest.__table__.c["decided_by_user_id"]
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "SET NULL"

    def test_user_id_cascade_on_user_delete(self):
        """If a sales-team member is hard-deleted, their extension
        requests should go with them — there's no audit value in
        keeping orphaned rows."""
        from app.models.work_time import WorkTimeExtensionRequest

        col = WorkTimeExtensionRequest.__table__.c["user_id"]
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_requester_relationship_is_lazy_joined(self):
        """``admin_list_requests`` relies on ``lazy="joined"`` so the
        N requests + N users come back in one query. Lazy="select"
        would silently re-introduce N+1 on the admin queue page.
        """
        from app.models.work_time import WorkTimeExtensionRequest

        rel = WorkTimeExtensionRequest.__mapper__.relationships["requester"]
        assert rel.lazy == "joined"


# ═════════════════════════════════════════════════════════════════════
# Migration coverage
# ═════════════════════════════════════════════════════════════════════


def test_migration_covers_all_user_columns_and_table():
    """The migration must touch all four user columns + the requests
    table. A model-vs-migration drift where someone adds a column to
    the model without writing the matching ``op.add_column`` would
    pass tests but explode on prod's ``alembic upgrade head``.
    """
    versions = (
        Path(__file__).resolve().parent.parent / "alembic" / "versions"
    )
    files = [
        f
        for f in versions.glob("*.py")
        if "work_time" in f.name or "work_window" in f.name
    ]
    assert files, "No alembic migration mentions work_time / work_window."

    blob = "\n".join(f.read_text() for f in files)
    for column in (
        "work_window_enabled",
        "work_window_start_min",
        "work_window_end_min",
        "work_window_override_until",
    ):
        assert column in blob, f"Migration is missing column: {column}"

    assert "work_time_extension_requests" in blob
    # Indexes that the admin queue + per-user history queries depend on.
    assert "ix_work_time_ext_status_requested" in blob
    assert "ix_work_time_ext_user_status_requested" in blob


def test_migration_has_idempotent_inspector_check():
    """Re-running ``alembic upgrade head`` after a partial apply must
    be a no-op. The ``_column_exists`` / ``_table_exists`` inspector
    pattern is what makes that safe."""
    versions = (
        Path(__file__).resolve().parent.parent / "alembic" / "versions"
    )
    target = next(versions.glob("*work_time_windows*.py"))
    src = target.read_text()
    assert "_column_exists" in src
    assert "_table_exists" in src


# ═════════════════════════════════════════════════════════════════════
# Handler source guards — the most cost-effective way to lock
# semantic invariants without booting a live HTTP harness.
# ═════════════════════════════════════════════════════════════════════


class TestHandlerInvariants:
    def _src(self, fn):
        return inspect.getsource(fn)

    def test_get_current_user_enforces_window_after_token_validation(self):
        """The 423 check must run AFTER the JWT decode + user load —
        otherwise an unauth'd request would 423 instead of 401, which
        would mislead operators investigating session loss as a
        work-window issue.
        """
        from app.api import deps

        src = self._src(deps.get_current_user)
        idx_user_load = src.find("scalar_one_or_none()")
        idx_window_check = src.find("user_can_access_now")
        assert idx_user_load > 0, "user load step missing"
        assert idx_window_check > 0, "work-window check missing"
        assert idx_window_check > idx_user_load, (
            "Work-window check runs before the user is loaded — anonymous "
            "requests would 423 instead of 401."
        )

    def test_get_current_user_admins_exempt_from_window(self):
        """Role check must short-circuit so admins reach admin UIs
        even when the window would otherwise apply."""
        from app.api import deps

        src = self._src(deps.get_current_user)
        # The check is "if user.role not in ('admin', 'super_admin'):"
        # which guards the entire 423 block.
        assert (
            'user.role not in ("admin", "super_admin")' in src
            or "user.role not in ('admin', 'super_admin')" in src
        ), "Admin exemption is missing from get_current_user."

    def test_get_current_user_returns_423_on_outside_window(self):
        """The exact status code matters — 401 would trigger the
        global frontend redirect to /login, 403 would render
        "permission denied" copy. Only 423 routes to the lock-out
        screen."""
        from app.api import deps

        src = self._src(deps.get_current_user)
        assert "HTTP_423_LOCKED" in src

    def test_create_extension_request_enforces_one_pending_at_a_time(self):
        """Anti-spam guard: 409 when a pending request already exists.
        Without this, a frustrated user could spam the admin queue."""
        from app.api.v1 import work_window

        src = self._src(work_window.create_my_extension_request)
        assert 'WorkTimeExtensionRequest.status == "pending"' in src
        assert "status_code=409" in src

    def test_admin_decide_request_rejects_non_pending(self):
        """Once decided, a request stays decided. Re-deciding a
        closed request would either silently noop (confusing) or
        re-bump the override (security hole). 409 makes the rule
        explicit."""
        from app.api.v1 import work_window

        src = self._src(work_window.admin_decide_request)
        assert 'req.status != "pending"' in src
        assert "status_code=409" in src

    def test_admin_decide_request_stacks_on_active_override(self):
        """If a user already has an active override, approving another
        request must extend FROM the existing override end, not from
        ``now``. The "anchor = max(now_utc, override_until)" pattern
        is what makes that work."""
        from app.api.v1 import work_window

        src = self._src(work_window.admin_decide_request)
        # The exact line is "anchor = target.work_window_override_until".
        assert "anchor = target.work_window_override_until" in src, (
            "admin_decide_request no longer stacks on active overrides — "
            "a second approval would shorten the user's access instead "
            "of extending it."
        )

    def test_admin_decide_request_freezes_approved_until(self):
        """The decision row stores the computed ``approved_until`` so
        a hypothetical re-approval can't silently re-extend the
        override beyond the original decision."""
        from app.api.v1 import work_window

        src = self._src(work_window.admin_decide_request)
        assert "req.approved_until = approved_until" in src

    def test_admin_update_window_uses_partial_patch_pattern(self):
        """Each field must be guarded with ``if body.X is not None:``
        so a partial PATCH doesn't accidentally null out the columns
        that were left unset."""
        from app.api.v1 import work_window

        src = self._src(work_window.admin_update_user_window)
        for field in ("body.enabled", "body.start_ist", "body.end_ist"):
            assert f"if {field} is not None" in src, (
                f"Partial PATCH guard missing for {field}. A request "
                f"that omits this field would null its column."
            )


# ═════════════════════════════════════════════════════════════════════
# Route table — every endpoint wired with the right HTTP method
# ═════════════════════════════════════════════════════════════════════


class TestRouteRegistration:
    """A typo on the HTTP method — e.g. ``@router.get`` where
    ``@router.post`` was intended — silently breaks the frontend
    without a Python-level error. This table-driven check is the
    smallest reliable guard."""

    EXPECTED_ROUTES = (
        ("GET", "/api/v1/work-window/me"),
        ("POST", "/api/v1/work-window/me/extension-requests"),
        ("GET", "/api/v1/work-window/me/extension-requests"),
        ("GET", "/api/v1/work-window/admin/users/{user_id}"),
        ("PATCH", "/api/v1/work-window/admin/users/{user_id}"),
        ("POST", "/api/v1/work-window/admin/users/{user_id}/override"),
        ("GET", "/api/v1/work-window/admin/extension-requests"),
        ("POST", "/api/v1/work-window/admin/extension-requests/{request_id}/decision"),
    )

    def test_every_route_present_with_correct_method(self):
        from tests._routes import registered_routes

        actual = registered_routes()
        for method, path in self.EXPECTED_ROUTES:
            assert (method, path) in actual, (
                f"Missing route: {method} {path}. Either it was "
                f"renamed (frontend will 404) or the HTTP method was "
                f"changed (frontend will get 405)."
            )

    def test_admin_routes_require_admin_role(self):
        """Every ``/work-window/admin/...`` handler must depend on
        ``require_role("admin")``. A reviewer who guessed the path
        should hit 403, not see the queue."""
        from app.api.v1 import work_window as ww_router

        admin_handlers = (
            ww_router.admin_get_user_window,
            ww_router.admin_update_user_window,
            ww_router.admin_set_override,
            ww_router.admin_list_requests,
            ww_router.admin_decide_request,
        )
        for handler in admin_handlers:
            src = inspect.getsource(handler)
            assert 'require_role("admin")' in src, (
                f"{handler.__name__} is missing require_role('admin'). "
                f"Reviewers/viewers could reach an admin endpoint."
            )


# ═════════════════════════════════════════════════════════════════════
# Behavioural edges on user_can_access_now
# ═════════════════════════════════════════════════════════════════════


def _user(**overrides):
    """User-shaped namespace covering every field the helper reads."""
    base = dict(
        work_window_enabled=True,
        work_window_start_min=540,
        work_window_end_min=1080,
        work_window_override_until=None,
        role="reviewer",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestAccessEdges:
    def test_naive_override_is_treated_as_utc(self):
        """A legacy/buggy code path that wrote a naive datetime to
        the override column shouldn't crash the comparison. The
        helper must coerce to UTC."""
        from app.utils.work_window import user_can_access_now

        # Naive — will be coerced.
        future_naive = datetime(2099, 1, 1, 0, 0)
        u = _user(work_window_override_until=future_naive)
        # 2026-04-27 00:00 UTC = 05:30 IST (outside default 09:00–18:00)
        # but the override is far in the future, so access is granted.
        now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
        assert user_can_access_now(u, now) is True

    def test_override_exactly_at_now_does_not_lift(self):
        """The check is strict ``>``, not ``>=`` — an override that
        ends exactly at the comparison instant is already over. Pins
        the boundary so a future relax to ``>=`` is intentional."""
        from app.utils.work_window import user_can_access_now

        now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
        u = _user(work_window_override_until=now)
        assert user_can_access_now(u, now) is False

    def test_admin_role_is_not_short_circuited_in_helper(self):
        """The helper is role-agnostic — admin exemption lives in
        ``deps.get_current_user`` so Celery tasks can call this
        helper without a request context. A regression that adds
        role-based escape into the helper would silently bypass
        enforcement on the API surface."""
        from app.utils.work_window import user_can_access_now

        # Admin user, but outside window AND no override → still locked
        # at the helper level. Routing layer is what exempts admins.
        u = _user(role="admin")
        now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)  # 05:30 IST
        assert user_can_access_now(u, now) is False, (
            "user_can_access_now should be role-agnostic. If admin "
            "exemption leaks into the helper, Celery tasks would no "
            "longer be able to reuse it for shift-aware scheduling."
        )

    def test_disabled_window_with_outside_time_passes(self):
        """Belt-and-braces: the early return on ``enabled=False`` must
        win even when both the time AND the override say "blocked"."""
        from app.utils.work_window import user_can_access_now

        u = _user(
            work_window_enabled=False,
            work_window_override_until=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        # 03:00 UTC = 08:30 IST (outside default 09:00–18:00).
        now = datetime(2026, 4, 27, 3, 0, tzinfo=timezone.utc)
        assert user_can_access_now(u, now) is True

    def test_within_window_with_past_override_still_passes(self):
        """An expired override must NOT block someone who is otherwise
        inside their window — the override is purely additive."""
        from app.utils.work_window import user_can_access_now

        # 06:00 UTC = 11:30 IST — squarely inside the 09:00–18:00 window.
        now = datetime(2026, 4, 27, 6, 0, tzinfo=timezone.utc)
        u = _user(
            work_window_override_until=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        assert user_can_access_now(u, now) is True


# ═════════════════════════════════════════════════════════════════════
# IST math — corner cases on the conversion + format helpers
# ═════════════════════════════════════════════════════════════════════


class TestISTMath:
    def test_midnight_utc_is_330_minutes_ist(self):
        """05:30 IST. Most-tested anchor — if this regresses, every
        other test on the file lights up."""
        from app.utils.work_window import utc_to_ist_minute

        assert (
            utc_to_ist_minute(datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc))
            == 5 * 60 + 30
        )

    def test_18_30_utc_is_midnight_ist(self):
        """The IST day rolls at 18:30 UTC. A regression in the offset
        would shift the boundary by hours."""
        from app.utils.work_window import utc_to_ist_minute

        assert (
            utc_to_ist_minute(
                datetime(2026, 4, 27, 18, 30, tzinfo=timezone.utc)
            )
            == 0
        )

    def test_format_minute_handles_modulo(self):
        """``format_minute_ist`` modulos by 1440 — passing 1500 must
        wrap to 01:00, not crash. Defensive against future callers
        that accidentally pass an unmodded sum."""
        from app.utils.work_window import format_minute_ist

        assert format_minute_ist(1440) == "00:00"
        assert format_minute_ist(1500) == "01:00"

    def test_zone_aware_input_with_non_utc_tz_normalises(self):
        """A datetime in a non-UTC timezone must still produce the
        right IST minute — the helper converts via UTC first.
        Catches a naive ``+IST_OFFSET`` shortcut that ignores the
        input's tz."""
        from datetime import timezone as dt_tz

        from app.utils.work_window import utc_to_ist_minute

        # 09:00 in PST (UTC-8) == 17:00 UTC == 22:30 IST == minute 1350.
        pst = dt_tz(timedelta(hours=-8))
        ts = datetime(2026, 4, 27, 9, 0, tzinfo=pst)
        assert utc_to_ist_minute(ts) == 22 * 60 + 30


# ═════════════════════════════════════════════════════════════════════
# Allowlist defense
# ═════════════════════════════════════════════════════════════════════


def test_allowlist_does_not_grant_admin_paths():
    """Admin endpoints must NOT be on the allowlist — they should
    only be reachable via the admin role exemption. Putting
    ``/work-window/admin/`` on the allowlist would let a locked-out
    reviewer hit admin endpoints (still 403'd by require_role, but
    the layered defence is the point)."""
    from app.api import deps

    for prefix in deps.WORK_WINDOW_ALLOWLIST_PREFIXES:
        assert "/admin" not in prefix, (
            f"Allowlist prefix {prefix!r} contains '/admin'. The admin "
            f"surface should be reached by role exemption, not by "
            f"prefix-based bypass."
        )


def test_allowlist_includes_logout_path():
    """A locked-out user must always be able to sign out — otherwise
    they can't clear their session to switch accounts. ``/auth/`` is
    the parent prefix; sanity-check that ``/auth/logout`` slots under
    it."""
    from app.api import deps

    matches = [
        p for p in deps.WORK_WINDOW_ALLOWLIST_PREFIXES
        if "/api/v1/auth/logout".startswith(p)
    ]
    assert matches, (
        "/api/v1/auth/logout is not on the work-window allowlist — "
        "a locked-out user can't sign out."
    )


# ═════════════════════════════════════════════════════════════════════
# Sidebar / route registration on the frontend side — file-existence
# probe so a future "page renamed" doesn't silently break the link.
# ═════════════════════════════════════════════════════════════════════


def test_frontend_pages_and_components_exist():
    """Cheap sanity probe that the migration's frontend counterparts
    are on disk. Catches a partial revert that drops the page file
    while leaving the route entry in App.tsx."""
    front = (
        Path(__file__).resolve().parent.parent.parent / "frontend" / "src"
    )
    assert (front / "pages" / "WorkWindowsPage.tsx").exists()
    assert (front / "components" / "WorkWindowGate.tsx").exists()


def test_frontend_app_routes_work_windows():
    front = (
        Path(__file__).resolve().parent.parent.parent / "frontend" / "src"
    )
    app_tsx = (front / "App.tsx").read_text()
    # Both the route and the import must be present — partial reverts
    # often drop one without the other.
    assert "WorkWindowsPage" in app_tsx
    assert '"/work-windows"' in app_tsx
    assert "WorkWindowGate" in app_tsx, (
        "App.tsx no longer wraps ProtectedLayout in WorkWindowGate — "
        "non-admins outside their shift would still see the app."
    )


def test_frontend_sidebar_lists_work_windows_under_admin():
    front = (
        Path(__file__).resolve().parent.parent.parent / "frontend" / "src"
    )
    sidebar = (front / "components" / "Sidebar.tsx").read_text()
    # The admin nav block must contain a "Work Windows" entry pointing
    # at the route. ``adminNavigation`` is the documented section.
    assert re.search(
        r'adminNavigation\s*=\s*\[[^\]]*"Work Windows"[^\]]*"/work-windows"',
        sidebar,
        re.DOTALL,
    ), "Sidebar admin nav missing 'Work Windows' → '/work-windows' entry."
