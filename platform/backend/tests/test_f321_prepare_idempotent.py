"""F321 — race-safe + idempotent /applications/prepare.

Pre-fix: a second ``POST /applications/prepare`` for the same
``(user_id, job_id)`` raised ``IntegrityError`` on
``UNIQUE(user_id, job_id)`` → bubbled as 500. Concurrent
double-clicks from the UI also raced past any check.

F321 contract:
  (a) **Idempotent re-prepare**: clicking Prepare twice on a
      ``status='prepared'`` job refreshes ``prepared_answers``
      + ``resume_id`` and returns the same row.
  (b) **No-downgrade**: clicking Prepare on a job whose
      Application is past prepared (applied / interview / offer)
      returns 409 with a clear message — don't reset the
      lifecycle.
  (c) **Race-safe**: SAVEPOINT + IntegrityError catch closes
      the residual race where 2 concurrent requests both pass
      the SELECT and reach INSERT.
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
os.environ.setdefault("JWT_SECRET", "pytest-f321")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "applications.py").read_text()


def test_prepare_checks_for_existing_application():
    """Pre-INSERT check for an existing Application by
    (user_id, job_id) — the lookup branch enables both
    idempotent re-prepare and the no-downgrade 409 path.
    """
    src = _read()
    handler_start = src.find("async def prepare_application")
    end = src.find("@router.", handler_start + 1)
    body = src[handler_start:end]
    assert "Application.user_id == user.id" in body
    assert "Application.job_id == job.id" in body, (
        "F321 regression: prepare_application no longer pre-"
        "checks for an existing Application. Idempotent re-"
        "prepare path is gone."
    )


def test_prepare_refreshes_existing_prepared_row():
    """Idempotent path: existing.status == 'prepared' →
    refresh ``prepared_answers`` + ``resume_id`` on that row
    instead of inserting a second."""
    src = _read()
    handler_start = src.find("async def prepare_application")
    end = src.find("@router.", handler_start + 1)
    body = src[handler_start:end]
    assert 'existing.status != "prepared"' in body
    assert "existing.prepared_answers = prepared_answers" in body
    assert "existing.resume_id = resume.id" in body


def test_prepare_returns_409_on_already_applied():
    """No-downgrade: existing application with status past
    'prepared' returns 409, not 500.
    """
    src = _read()
    handler_start = src.find("async def prepare_application")
    end = src.find("@router.", handler_start + 1)
    body = src[handler_start:end]
    assert "status_code=409" in body, (
        "F321 regression: prepare_application no longer 409s "
        "when the job is past 'prepared' state. A user who "
        "applied 3 weeks ago and accidentally clicks Prepare "
        "again would now downgrade their lifecycle."
    )


def test_prepare_wraps_insert_in_savepoint():
    """SAVEPOINT + IntegrityError catch is the race-safe gate
    for the residual concurrent-insert case.
    """
    src = _read()
    handler_start = src.find("async def prepare_application")
    end = src.find("@router.", handler_start + 1)
    body = src[handler_start:end]
    assert "db.begin_nested()" in body, (
        "F321 regression: INSERT path no longer wraps in a "
        "SAVEPOINT. Concurrent /prepare clicks will 500 again."
    )
    assert "except IntegrityError" in body, (
        "F321 regression: race-recovery branch removed."
    )
