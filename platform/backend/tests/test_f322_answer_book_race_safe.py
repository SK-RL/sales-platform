"""F322 — race-safe AnswerBookEntry create on concurrent POSTs.

Pre-fix the lookup-then-INSERT in ``create_answer`` had a TOCTOU
window — two browser tabs / double-clicks could both pass the
dup check, both reach INSERT, the second hits
``uq_answer_user_resume_key`` and 500s.

F322 wraps the INSERT in ``db.begin_nested()`` (SAVEPOINT) and
catches the IntegrityError, re-raising it as the same 409 the
handler-check returns. Constraint is the only race-safe gate;
application-level check stays as the fast path.
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
os.environ.setdefault("JWT_SECRET", "pytest-f322")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_create_answer_uses_savepoint():
    src = (_BACKEND / "app" / "api" / "v1" / "answer_book.py").read_text()
    handler_start = src.find("async def create_answer")
    end = src.find("@router.", handler_start + 1)
    body = src[handler_start:end]
    assert "db.begin_nested()" in body, (
        "F322 regression: create_answer no longer wraps INSERT "
        "in a SAVEPOINT. Concurrent POSTs from the same user "
        "will 500 again on uq_answer_user_resume_key."
    )


def test_create_answer_translates_race_to_409():
    src = (_BACKEND / "app" / "api" / "v1" / "answer_book.py").read_text()
    handler_start = src.find("async def create_answer")
    end = src.find("@router.", handler_start + 1)
    body = src[handler_start:end]
    assert "except IntegrityError" in body, (
        "F322 regression: race-recovery branch removed."
    )
    # Race path returns 409, matching the handler-check 409 shape
    assert body.count("status_code=409") >= 2, (
        "F322 regression: race-recovery branch no longer 409s. "
        "Either the handler-check 409 or the race-recovery 409 "
        "is missing — both must be present so the user-visible "
        "outcome is the same regardless of timing."
    )
