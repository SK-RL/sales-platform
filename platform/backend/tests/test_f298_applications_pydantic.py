"""F298 — Pydantic schemas for the last two ``body: dict`` handlers
in applications.py.

Pre-fix ``POST /applications/prepare`` and
``POST /applications/{id}/sync-answers`` took ``body: dict`` with
``body.get(...)`` field extraction. Same F128 pattern as F147 +
F287: typos like ``jb_id`` silently dropped, unknown fields
ignored, no length caps, no per-item validation.

F298 reshapes both inputs as Pydantic models with
``extra="forbid"`` + Field-level constraints.
"""
from __future__ import annotations

import os
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
os.environ.setdefault("JWT_SECRET", "pytest-f298")


def test_prepare_application_request_requires_job_id():
    from app.api.v1.applications import PrepareApplicationRequest
    import pydantic

    PrepareApplicationRequest(job_id="00000000-0000-0000-0000-000000000001")

    with pytest.raises(pydantic.ValidationError):
        PrepareApplicationRequest()  # type: ignore[call-arg]


def test_prepare_application_request_rejects_extra_fields():
    """``extra="forbid"`` means typos like ``jb_id`` 422 instead of
    silently dropping."""
    from app.api.v1.applications import PrepareApplicationRequest
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PrepareApplicationRequest(  # type: ignore[call-arg]
            job_id="00000000-0000-0000-0000-000000000001",
            jb_id="typo",
        )


def test_prepare_application_rejects_non_uuid_job_id():
    from app.api.v1.applications import PrepareApplicationRequest
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PrepareApplicationRequest(job_id="not-a-uuid")


def test_sync_answers_request_caps_answer_list():
    """200 items max — DoS bound."""
    from app.api.v1.applications import SyncAnswersRequest, SyncAnswerItem
    import pydantic

    # 200 items pass.
    answers = [
        SyncAnswerItem(question_key=f"q_{i}", answer="x") for i in range(200)
    ]
    SyncAnswersRequest(answers=answers)

    # 201 items rejected.
    with pytest.raises(pydantic.ValidationError):
        oversized = [
            SyncAnswerItem(question_key=f"q_{i}", answer="x") for i in range(201)
        ]
        SyncAnswersRequest(answers=oversized)


def test_sync_answer_item_caps_field_lengths():
    from app.api.v1.applications import SyncAnswerItem
    import pydantic

    SyncAnswerItem(question_key="ok", answer="ok")

    # 201-char question_key rejected
    with pytest.raises(pydantic.ValidationError):
        SyncAnswerItem(question_key="x" * 201, answer="ok")
    # 5001-char answer rejected
    with pytest.raises(pydantic.ValidationError):
        SyncAnswerItem(question_key="ok", answer="a" * 5001)
    # Empty question_key rejected (min_length=1)
    with pytest.raises(pydantic.ValidationError):
        SyncAnswerItem(question_key="", answer="ok")


def test_sync_answers_rejects_extra_fields():
    from app.api.v1.applications import SyncAnswersRequest
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        SyncAnswersRequest(answers=[], extra_field="oops")  # type: ignore[call-arg]


def test_handlers_use_pydantic_body_not_dict():
    """Source-level guard — ensure no future refactor reverts to
    ``body: dict``. We check the signature LINE (first occurrence
    after ``async def``) rather than the whole handler body, since
    the handler docstring legitimately mentions ``body: dict`` as
    the historical pattern.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "api" / "v1" / "applications.py").read_text()
    for handler_name, expected in (
        ("prepare_application", "PrepareApplicationRequest"),
        ("sync_answers_to_book", "SyncAnswersRequest"),
    ):
        handler_start = src.find(f"async def {handler_name}")
        assert handler_start > 0
        # Slice the SIGNATURE only — from ``async def`` to the
        # ``):`` that closes the parameter list.
        sig_end = src.find("):\n", handler_start)
        sig = src[handler_start:sig_end + 3]
        assert "body: dict" not in sig, (
            f"F298 regression: {handler_name} signature reverted to "
            f"``body: dict``. F128 pattern reopens. Sig: {sig!r}"
        )
        assert f"body: {expected}" in sig, (
            f"F298 regression: {handler_name} no longer takes "
            f"``body: {expected}``. Pydantic validation bypassed."
        )
