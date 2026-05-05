"""F326 — race-safe ``POST /auth/register``.

Pre-fix the registration handler did a lookup-then-INSERT
pattern: SELECT for an existing user by email, raise 409 if one
exists, otherwise db.add(User(...)) + commit. Two concurrent
POSTs with the same email (form double-submit on slow connection,
script POSTing in a tight loop) both passed the lookup, both
INSERTed, and the second blew up with an unhandled
IntegrityError on the ``users.email`` UNIQUE constraint that
escaped to the client as a bare HTTP 500.

F326 wraps the commit in try/except IntegrityError, matches on
the ``users_email`` constraint name (Postgres autogenerates
``users_email_key`` for unnamed UNIQUE-on-column constraints
per the ``<table>_<column>_key`` convention), and re-raises as
the same 409 the lookup-check produces.

The match is intentionally a substring check (``users_email``
not exactly ``users_email_key``) so a future migration that
renames the constraint to ``uq_users_email`` doesn't silently
regress.
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
os.environ.setdefault("JWT_SECRET", "pytest-f326")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "auth.py").read_text()


def _read_model() -> str:
    return (_BACKEND / "app" / "models" / "user.py").read_text()


def test_handler_imports_integrity_error():
    src = _read_handler()
    assert "from sqlalchemy.exc import IntegrityError" in src, (
        "F326 regression: IntegrityError import was removed from "
        "auth.py — race-safe except can no longer fire."
    )


def test_register_handler_wraps_commit_in_try_except():
    """The race-safe block must wrap ``await db.commit()`` after
    ``db.add(new_user)`` in the register handler.
    """
    src = _read_handler()
    # Anchor on the User add immediately followed by the commit
    add_idx = src.find("db.add(new_user)")
    assert add_idx > 0, "register handler structure changed; F326 anchor lost"
    window = src[add_idx:add_idx + 3500]
    assert "try:" in window, (
        "F326 regression: db.add(new_user) is no longer followed "
        "by a try block. Concurrent same-email registrations 500 again."
    )
    assert "except IntegrityError" in window, (
        "F326 regression: race-recovery branch removed from register."
    )


def test_register_constraint_name_match_present():
    """The translation MUST be gated on the email-specific constraint
    so non-email IntegrityErrors don't get hidden as 409s.
    """
    src = _read_handler()
    add_idx = src.find("db.add(new_user)")
    window = src[add_idx:add_idx + 3500]
    assert "users_email" in window, (
        "F326 regression: constraint-name match removed. The handler "
        "would now translate ALL IntegrityErrors to 409 and hide "
        "genuinely-different bugs (FK violations, etc.)."
    )


def test_register_409_message_byte_identical_to_lookup_check():
    """The race-branch 409 must have the same body as the
    lookup-check 409 so concurrent vs serial duplicates surface
    identically to the client.
    """
    src = _read_handler()
    # The lookup-check 409 is at line ~257
    assert 'detail="Email already registered"' in src
    # The race branch must reuse the exact phrase
    add_idx = src.find("db.add(new_user)")
    window = src[add_idx:add_idx + 3500]
    assert 'detail="Email already registered"' in window, (
        "F326 regression: race branch 409 body diverges from the "
        "lookup-check 409 body."
    )


def test_register_does_not_blanket_translate_integrity_errors():
    """If the IntegrityError isn't on ``users_email``, the handler
    must re-raise so the worker logs the underlying issue instead
    of silently returning a misleading 409.
    """
    src = _read_handler()
    add_idx = src.find("db.add(new_user)")
    window = src[add_idx:add_idx + 3500]
    # The branch ends with a bare ``raise`` to propagate other errors.
    # We look for the pattern ``        raise`` near the bottom of the
    # except block.
    assert "        raise\n" in window, (
        "F326 regression: race branch swallows ALL IntegrityErrors as "
        "409. Only the email-collision case should translate; other "
        "constraint failures must propagate so they get logged."
    )


def test_user_model_email_unique_intact():
    """The whole F326 fix only matters if the underlying UNIQUE is
    still in place. Guard against a future schema relaxation.
    """
    src = _read_model()
    assert 'email:' in src
    assert 'unique=True' in src, (
        "F326 regression: User.email UNIQUE constraint dropped — "
        "the race fix is now moot but duplicate emails will silently "
        "succeed at the DB layer."
    )
