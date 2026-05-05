"""F327 — race-safe ``POST /pipeline/stages`` admin endpoint.

Pre-fix the create_stage handler did a lookup-then-INSERT
pattern: SELECT for an existing stage by ``key``, raise 400 if
one exists, otherwise db.add(PipelineStage(...)) + commit. Two
concurrent admin POSTs with the same key both passed the lookup,
both INSERTed, and the second blew up with an unhandled
IntegrityError on the ``pipeline_stages.key`` UNIQUE constraint
that escaped to the client as a bare HTTP 500.

F327 wraps the commit in try/except IntegrityError, matches on
the ``pipeline_stages_key`` substring (Postgres autogenerates
``pipeline_stages_key_key`` for unnamed UNIQUE-on-column
constraints), and re-raises as the same HTTP 400 "Stage key
already exists" the lookup-check produces.
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
os.environ.setdefault("JWT_SECRET", "pytest-f327")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "pipeline.py").read_text()


def _read_model() -> str:
    return (_BACKEND / "app" / "models" / "pipeline_stage.py").read_text()


def test_handler_imports_integrity_error():
    src = _read_handler()
    assert "from sqlalchemy.exc import IntegrityError" in src, (
        "F327 regression: IntegrityError import was removed from "
        "pipeline.py — race-safe except can no longer fire."
    )


def test_create_stage_handler_wraps_commit_in_try_except():
    src = _read_handler()
    # There are TWO ``db.add(stage)`` call sites in pipeline.py —
    # the default-stage seeder in ``_get_active_stages`` and the
    # F327-protected create handler. Anchor on the create_stage
    # handler so we don't accidentally match the seeder window.
    handler_idx = src.find("async def create_stage")
    assert handler_idx > 0, "create_stage handler structure changed"
    add_idx = src.find("db.add(stage)", handler_idx)
    assert add_idx > 0
    window = src[add_idx:add_idx + 3000]
    assert "try:" in window, (
        "F327 regression: db.add(stage) is no longer followed by a "
        "try block. Concurrent same-key POSTs will 500 again."
    )
    assert "except IntegrityError" in window, (
        "F327 regression: race-recovery branch removed."
    )


def test_create_stage_constraint_name_match_present():
    src = _read_handler()
    add_idx = src.find("db.add(stage)")
    window = src[add_idx:add_idx + 3000]
    assert "pipeline_stages_key" in window, (
        "F327 regression: constraint-name match removed. The handler "
        "would now translate ALL IntegrityErrors to 400 and hide "
        "genuinely-different bugs."
    )


def test_create_stage_400_message_byte_identical_to_lookup_check():
    src = _read_handler()
    # Lookup-check 400 uses ``"Stage key already exists"`` (positional
    # detail, not detail=...).
    assert '"Stage key already exists"' in src
    handler_idx = src.find("async def create_stage")
    add_idx = src.find("db.add(stage)", handler_idx)
    window = src[add_idx:add_idx + 3000]
    assert '"Stage key already exists"' in window, (
        "F327 regression: race branch 400 message diverges from the "
        "lookup-check 400 message."
    )


def test_create_stage_does_not_blanket_translate_integrity_errors():
    src = _read_handler()
    handler_idx = src.find("async def create_stage")
    add_idx = src.find("db.add(stage)", handler_idx)
    window = src[add_idx:add_idx + 3000]
    assert "        raise\n" in window, (
        "F327 regression: race branch swallows ALL IntegrityErrors as "
        "400. Only the key-collision case should translate; other "
        "constraint failures must propagate."
    )


def test_pipeline_stage_model_key_unique_intact():
    src = _read_model()
    assert "key:" in src
    assert "unique=True" in src, (
        "F327 regression: PipelineStage.key UNIQUE constraint dropped "
        "— the race fix is now moot but duplicate keys will silently "
        "succeed at the DB layer."
    )
