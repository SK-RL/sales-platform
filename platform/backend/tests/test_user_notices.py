"""In-app login notices — routes, user-scoping, migration chain.

Backs the admin->user banner (app/api/v1/notices.py) first used to tell
the two admins who manage the KYC profiles to re-upload the documents
lost in the storage incident (feedback 650514ad). Source-level guards
matching the suite's no-live-DB style.
"""

from __future__ import annotations

import inspect
import os
import pathlib
import re

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-notices")


def test_notice_routes_registered():
    from app.api.v1.router import api_router

    paths = {
        (m, r.path)
        for r in api_router.routes
        for m in (getattr(r, "methods", None) or set())
    }
    assert ("GET", "/api/v1/notices/me") in paths
    assert ("POST", "/api/v1/notices/{notice_id}/dismiss") in paths


def test_feed_is_user_scoped_and_undismissed_only():
    """A user must only ever see their OWN undismissed notices — never
    another user's, and never ones they already dismissed."""
    from app.api.v1.notices import list_my_notices

    src = inspect.getsource(list_my_notices)
    assert "UserNotice.user_id == user.id" in src, (
        "notices feed must filter by the caller's user id (cross-user leak)"
    )
    assert "dismissed_at.is_(None)" in src


def test_dismiss_is_scoped_to_owner():
    from app.api.v1.notices import dismiss_notice

    src = inspect.getsource(dismiss_notice)
    # Both the id AND the owner must be in the WHERE so you can't dismiss
    # (or probe the existence of) someone else's notice.
    assert "UserNotice.id == notice_id" in src
    assert "UserNotice.user_id == user.id" in src


def test_model_registered():
    import app.models as models

    assert hasattr(models, "UserNotice")
    assert "UserNotice" in models.__all__


def test_migration_chains_onto_prior_head():
    """The new migration must descend from the head it was written
    against (q3r4s5t6u7v8) so ``alembic upgrade head`` applies it."""
    mig = (
        pathlib.Path(__file__).resolve().parent.parent
        / "alembic" / "versions"
        / "2026_08_05_r5s6t7u8v9w0_user_notices.py"
    ).read_text()
    assert re.search(r'revision\s*=\s*"r5s6t7u8v9w0"', mig)
    assert re.search(r'down_revision\s*=\s*"q3r4s5t6u7v8"', mig)
    # Guarded create so a re-run is a no-op, not a crash.
    assert "get_table_names()" in mig and "user_notices" in mig
