"""F292 — FeedbackUpdate.status/priority Literal typing (closes F162 b).

F162(b) noted ``FeedbackUpdate`` had no Literal constraint on
``status`` / ``priority`` — they were bare ``str | None``.
The handler's ``VALID_STATUSES`` / ``VALID_PRIORITIES`` check
caught non-canonical values at handler-time but only on endpoints
that explicitly invoked the check. The Literal makes the contract
enforced at parse time across every PATCH path.
"""
from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f292")


def test_feedback_update_status_rejects_non_canonical():
    from app.schemas.feedback import FeedbackUpdate
    import pydantic

    # Canonical values pass.
    for status in ("open", "in_progress", "resolved", "closed"):
        u = FeedbackUpdate(status=status)
        assert u.status == status

    for bad in ("hacked", "ARBITRARY", "OPEN", "  open  "):
        try:
            FeedbackUpdate(status=bad)
        except pydantic.ValidationError:
            continue
        raise AssertionError(
            f"F292 regression: FeedbackUpdate.status accepted "
            f"non-canonical value {bad!r}. Literal constraint gone."
        )


def test_feedback_update_priority_rejects_non_canonical():
    from app.schemas.feedback import FeedbackUpdate
    import pydantic

    for priority in ("low", "medium", "high", "critical"):
        u = FeedbackUpdate(priority=priority)
        assert u.priority == priority

    for bad in ("URGENT", "p0", "EMERGENCY"):
        try:
            FeedbackUpdate(priority=bad)
        except pydantic.ValidationError:
            continue
        raise AssertionError(
            f"F292 regression: FeedbackUpdate.priority accepted "
            f"non-canonical value {bad!r}."
        )


def test_feedback_update_allows_partial_updates():
    """Status / priority remain Optional — partial updates that
    only set ``admin_notes`` must still pass.
    """
    from app.schemas.feedback import FeedbackUpdate

    u = FeedbackUpdate(admin_notes="something happened")
    assert u.status is None
    assert u.priority is None
    assert u.admin_notes == "something happened"
