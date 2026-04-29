"""F291 — /jobs/{id}/description routes through shared
extract_description helper (closes F122).

F122 found the inline raw_json fallback in
``api/v1/jobs.py::get_job_description`` had drifted from the
shared ``utils/job_description.py::extract_description`` that
the scan pipeline + ``backfill_job_descriptions`` use. Drift
caused: smartrecruiters jobs (structured sections) returned
empty; workable ``full_description`` missed; bamboohr's
``jobOpeningDescription`` missed; career-page rows didn't render.

F291 replaces the per-key inline fallback with a single call to
``extract_description(platform, raw_json)`` so both write-side
(scan task) and read-side (this endpoint) honour the same
per-platform mapping.
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
os.environ.setdefault("JWT_SECRET", "pytest-f291")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "jobs.py").read_text()


def test_handler_imports_extract_description():
    """The handler must import the shared helper. The whole point
    is to use ONE source of truth for per-platform field mapping.
    """
    src = _read()
    handler_start = src.find("async def get_job_description")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end] if handler_end > 0 else src[handler_start:]
    assert "from app.utils.job_description import extract_description" in handler, (
        "F291 regression: ``get_job_description`` no longer imports "
        "the shared ``extract_description`` helper. Drift between "
        "scan-side and read-side description handling reopens."
    )


def test_handler_no_longer_has_inline_per_key_fallback():
    """The hand-rolled ``raw.get('content') or raw.get(...)`` chain
    was the bug — it diverged from the shared helper. The handler
    must NOT have that pattern anymore.
    """
    src = _read()
    handler_start = src.find("async def get_job_description")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end] if handler_end > 0 else src[handler_start:]
    # The smoking gun pattern is multiple ``raw.get(`` calls
    # chained with ``or``. Three or more such chained calls means
    # the inline per-key fallback is back.
    raw_get_count = handler.count("raw.get(")
    assert raw_get_count <= 1, (
        f"F291 regression: ``get_job_description`` has {raw_get_count} "
        f"``raw.get(...)`` calls — the inline per-key fallback "
        f"chain is back. Use the shared helper instead."
    )


def test_handler_calls_extract_description_with_platform_and_raw():
    """The helper signature is ``extract_description(platform: str,
    raw_json: dict | None) -> tuple[str, str]`` — handler must
    pass both arguments.
    """
    src = _read()
    handler_start = src.find("async def get_job_description")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end] if handler_end > 0 else src[handler_start:]
    assert "extract_description(" in handler, (
        "F291 regression: handler no longer invokes "
        "``extract_description``. The fallback is now a no-op."
    )
    # Must pass the platform — pre-fix the inline fallback ignored
    # the platform field, which is precisely why it diverged from
    # the per-platform-keyed shared helper.
    assert "job.platform" in handler, (
        "F291 regression: handler no longer passes "
        "``job.platform`` to the helper. Per-platform key mapping "
        "won't fire."
    )
