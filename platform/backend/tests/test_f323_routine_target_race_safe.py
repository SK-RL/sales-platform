"""F323 — race-safe RoutineTarget upsert.

Pre-fix the lookup-then-INSERT path was TOCTOU-vulnerable. Two
concurrent POSTs (two tabs queuing the same job, double-click)
both pass the SELECT, both add a RoutineTarget(user_id=X,
job_id=Y), the second hits ``uq_routine_targets_user_job`` and
500s.

F323 wraps the INSERT in ``db.begin_nested()`` (SAVEPOINT) and
catches IntegrityError. On race-loss the handler re-fetches the
winning RoutineTarget and applies the same idempotent
intent/note refresh that the existing-row branch does — most-
recent-write semantics.
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
os.environ.setdefault("JWT_SECRET", "pytest-f323")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "routine.py").read_text()


def test_target_handler_uses_savepoint():
    src = _read()
    rt_idx = src.find("RoutineTarget(")
    assert rt_idx > 0
    # Look at a wide enough window — the INSERT body is followed
    # by db.add then the savepoint wrap, then the IntegrityError
    # branch, all within ~2KB.
    window = src[max(0, rt_idx - 500):rt_idx + 2000]
    assert "db.begin_nested()" in window, (
        "F323 regression: RoutineTarget INSERT no longer wraps "
        "in a SAVEPOINT. Concurrent queue clicks will 500 again."
    )


def test_target_handler_catches_integrity_error():
    src = _read()
    rt_idx = src.find("RoutineTarget(")
    window = src[max(0, rt_idx - 500):rt_idx + 1500]
    assert "except IntegrityError" in window, (
        "F323 regression: race-recovery branch removed."
    )
    # Race-recovery re-fetches by the same key the constraint
    # uses
    assert "RoutineTarget.user_id" in window
    assert "RoutineTarget.job_id" in window
