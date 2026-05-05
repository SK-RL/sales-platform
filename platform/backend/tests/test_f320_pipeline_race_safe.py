"""F320 — race-safe PotentialClient auto-create on review accept.

Pre-fix: 2 reviewers concurrently accepting jobs from the same
company both pass the ``SELECT PotentialClient WHERE company_id=X``
lookup (both see no existing), both ``db.add(PotentialClient(...))``,
``UNIQUE(company_id)`` fires on the second commit. The F281
IntegrityError catch only matches ``uq_reviews_job_reviewer`` and
re-raises as a 500.

F320 wraps the PotentialClient insert in a SAVEPOINT and catches
the constraint violation. On race-loss, the handler re-fetches
the winning client and continues. ``company.is_target = True`` is
idempotent so safe on either path.
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
os.environ.setdefault("JWT_SECRET", "pytest-f320")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_handler_wraps_insert_in_savepoint():
    """The PotentialClient INSERT must live inside a
    ``db.begin_nested()`` SAVEPOINT so a constraint violation on
    commit doesn't poison the outer transaction.
    """
    src = (_BACKEND / "app" / "api" / "v1" / "reviews.py").read_text()
    submit_idx = src.find("async def submit_review")
    end = src.find("@router.", submit_idx + 1)
    body = src[submit_idx:end]
    assert "db.begin_nested()" in body, (
        "F320 regression: PotentialClient insert no longer wraps "
        "in a SAVEPOINT. Concurrent accepts on the same company "
        "will 500 again on the UNIQUE constraint."
    )


def test_handler_catches_integrity_error_on_pipeline_race():
    src = (_BACKEND / "app" / "api" / "v1" / "reviews.py").read_text()
    submit_idx = src.find("async def submit_review")
    end = src.find("@router.", submit_idx + 1)
    body = src[submit_idx:end]
    # IntegrityError catch + race-recovery refetch
    assert "except IntegrityError" in body
    assert "PotentialClient.company_id == job.company_id" in body, (
        "F320 regression: race-recovery branch no longer re-"
        "fetches the winning PotentialClient."
    )


def test_is_target_set_unconditionally_on_accept():
    """``company.is_target = True`` must run regardless of which
    reviewer won the race — both paths converge on the same
    company-target outcome.
    """
    src = (_BACKEND / "app" / "api" / "v1" / "reviews.py").read_text()
    submit_idx = src.find("async def submit_review")
    end = src.find("@router.", submit_idx + 1)
    body = src[submit_idx:end]
    # is_target = True must appear ONCE in the accept branch
    # (set BEFORE the savepoint enters so even race-losers stamp
    # the target flag)
    assert body.count("company.is_target = True") >= 1, (
        "F320 regression: is_target = True no longer set when a "
        "review is accepted. Auto-targeting based on review "
        "history breaks."
    )


def test_apply_handler_also_race_safe():
    """The /reviews/apply handler has the SAME PotentialClient
    lookup-then-insert pattern. Both endpoints must be race-safe
    or 2 users applying to jobs from the same company through
    different paths can still 500.
    """
    src = (_BACKEND / "app" / "api" / "v1" / "reviews.py").read_text()
    apply_idx = src.find("async def apply_from_review")
    assert apply_idx > 0
    end = src.find("@router.", apply_idx + 1)
    if end < 0:
        end = len(src)
    body = src[apply_idx:end]
    assert "db.begin_nested()" in body, (
        "F320 regression: /reviews/apply handler no longer wraps "
        "PotentialClient insert in a SAVEPOINT. Concurrent applies "
        "from same company will 500 again."
    )
    assert "except IntegrityError" in body, (
        "F320 regression: /reviews/apply no longer catches "
        "IntegrityError — race-recovery dies."
    )
