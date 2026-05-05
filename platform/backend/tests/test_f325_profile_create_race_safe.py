"""F325 — race-safe Profile create.

Pre-fix the lookup-then-INSERT path was TOCTOU-vulnerable. Two
concurrent ``POST /api/v1/profiles`` requests with the same
email both passed the case-insensitive ``SELECT`` lookup, both
``db.add(Profile(...))`` and the second INSERT blew up with an
unhandled ``IntegrityError`` on ``uq_profiles_email`` that
escaped to the client as a bare HTTP 500 plain-text body.

F325 wraps the INSERT under the existing ``await db.commit()``
in a ``try/except IntegrityError`` and re-raises as the same
409 the lookup-check produces — user-visible outcome identical
regardless of timing. Matches the pattern shipped in F281
(reviews), F282 (contacts), F316 (jobs), F322 (answer-book),
F323 (routine-targets), F324 (work-time pending).

The model declares the named constraint
``UniqueConstraint("email", name="uq_profiles_email")`` so the
handler can match on constraint name and avoid hiding genuinely-
different IntegrityErrors (FK violations, check-constraint
failures) behind the 409 translation.
"""
from __future__ import annotations

import os
import pathlib

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f325")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "profiles.py").read_text()


def _read_model() -> str:
    return (_BACKEND / "app" / "models" / "profile.py").read_text()


def test_handler_imports_integrity_error():
    """The IntegrityError translation requires the symbol to be in
    scope. F325 added the import at the top of profiles.py.
    """
    src = _read_handler()
    assert "from sqlalchemy.exc import IntegrityError" in src, (
        "F325 regression: IntegrityError import was removed from "
        "profiles.py. The race-safe except branch can no longer fire."
    )


def test_create_profile_handler_catches_integrity_error():
    """The race-safe block lives between ``db.add(profile)`` and
    ``db.refresh(profile)`` in ``create_profile``.
    """
    src = _read_handler()
    # Find the create_profile handler body — the one with both
    # ``db.add(profile)`` and a 409 raise nearby.
    add_idx = src.find("db.add(profile)")
    assert add_idx > 0
    # Race-safe wrap is within the next ~2KB of source after add.
    window = src[add_idx:add_idx + 2500]
    assert "try:" in window, (
        "F325 regression: db.add(profile) is no longer followed by a "
        "try block. Concurrent same-email POSTs will 500 again."
    )
    assert "except IntegrityError" in window, (
        "F325 regression: race-recovery branch removed."
    )
    assert "uq_profiles_email" in window, (
        "F325 regression: constraint-name match removed. The handler "
        "would now translate ALL IntegrityErrors to 409 and hide "
        "genuinely-different bugs (FK violations, etc.)."
    )


def test_create_profile_409_message_unchanged():
    """The 409 produced by the race branch must be byte-identical to
    the one the lookup-check produces — otherwise concurrent vs
    serial duplicates would surface as different errors and break
    client-side error-message matching.
    """
    src = _read_handler()
    add_idx = src.find("db.add(profile)")
    assert add_idx > 0
    window = src[add_idx:add_idx + 2500]
    # The lookup-check uses
    # ``f"A profile with email {body.email!r} already exists."``
    # The race branch must echo it.
    assert "A profile with email" in window
    assert "already exists" in window


def test_create_profile_does_not_leak_existing_id():
    """F238(e) coverage stays alive: the 409 detail must not include
    the existing row's UUID.
    """
    src = _read_handler()
    add_idx = src.find("db.add(profile)")
    window = src[add_idx:add_idx + 2500]
    # The pre-F238(e) form was ``... already exists (id={existing.id}).``
    # If that string ever returns, F238(e) regressed.
    assert "id={existing.id}" not in window, (
        "F238(e) regression: 409 detail leaks the existing profile UUID."
    )
    assert "id={" not in window, (
        "F238(e) regression: 409 detail interpolates an id into the "
        "body — drop the suffix."
    )


def test_constraint_named_on_model():
    """The handler matches on the constraint NAME, so the constraint
    must keep that exact name on the model. A drift here would let
    PG generate an autogen name like ``profiles_email_key`` and the
    409 translation would silently miss.
    """
    src = _read_model()
    assert 'name="uq_profiles_email"' in src, (
        "F325 regression: profiles.email UNIQUE constraint name "
        "drifted away from 'uq_profiles_email'. Either rename the "
        "constraint back or update the handler match string."
    )
    assert 'UniqueConstraint("email"' in src, (
        "F325 regression: profiles UNIQUE on email column dropped."
    )


def test_lookup_check_still_present_as_fast_path():
    """The race-safe except is the SAFETY net; the lookup-check is
    the FAST path for serial requests. We don't want the handler
    silently relying on the DB to detect every duplicate (would
    waste a transaction per dupe). Both paths must coexist.
    """
    src = _read_handler()
    # The lookup uses func.lower(Profile.email) == body.email.lower().
    # There are TWO call-sites of ``func.lower(Profile.email)`` in
    # the file — the search-filter on the list endpoint (uses
    # ``ilike``) and the duplicate-check in create_profile (uses
    # ``==``). We want the latter, so anchor on the equality form.
    assert "func.lower(Profile.email) == body.email.lower()" in src
    lookup_idx = src.find("func.lower(Profile.email) == body.email.lower()")
    assert lookup_idx > 0
    window = src[lookup_idx:lookup_idx + 1500]
    assert "if existing:" in window
    assert "status_code=409" in window
