"""F301 — skill-gaps raw_json fallback (closes F109).

F109 found ``GET /intelligence/skill-gaps`` returned
``jobs_analyzed=0`` despite the user having a resume + thousands of
relevant jobs in the DB. Root cause: the query did an INNER JOIN
against JobDescription, so jobs whose ``text_content`` was empty
(or whose JobDescription row didn't exist) silently dropped out.
At F109 verification time ~80% of historical rows had empty
JobDescription.

F301 mirrors the resume-scorer fallback (F97):
  1. SELECT BOTH ``text_content`` AND ``Job.raw_json`` + platform.
  2. Switch INNER JOIN → LEFT OUTER JOIN so jobs without a
     JobDescription row at all still appear.
  3. When ``text_content`` is empty, call the shared
     ``extract_description(platform, raw_json)`` helper to extract
     text on-the-fly. Same helper used by the scan pipeline,
     ``backfill_job_descriptions``, ``/jobs/{id}/description`` (F291),
     and the resume scorer — single source of truth.
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
os.environ.setdefault("JWT_SECRET", "pytest-f301")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "intelligence.py").read_text()


def test_skill_gaps_query_uses_outer_join():
    """The query must LEFT OUTER JOIN on JobDescription, not INNER
    JOIN — otherwise jobs without a JobDescription row silently
    drop out (the F109 root cause).
    """
    src = _read()
    handler_start = src.find("async def skill_gaps")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    # Pre-fix used `.join(Job, JobDescription.job_id == Job.id)`.
    # Post-fix uses `.outerjoin(JobDescription, ...)`.
    assert ".outerjoin(JobDescription," in handler, (
        "F301 regression: skill_gaps query no longer uses "
        "``.outerjoin(JobDescription, ...)``. INNER JOIN drops "
        "rows whose JobDescription is missing — F109 reopens."
    )


def test_skill_gaps_query_selects_raw_json_and_platform():
    """The fallback needs ``Job.raw_json`` + ``Job.platform`` to
    invoke ``extract_description`` per row.
    """
    src = _read()
    handler_start = src.find("async def skill_gaps")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "Job.raw_json" in handler, (
        "F301 regression: ``Job.raw_json`` no longer in the SELECT. "
        "Fallback can't extract text from it."
    )
    assert "Job.platform" in handler, (
        "F301 regression: ``Job.platform`` no longer in the SELECT. "
        "extract_description needs the platform key for per-platform "
        "fallback maps."
    )


def test_skill_gaps_calls_extract_description():
    """The fallback path must invoke the shared helper. A future
    refactor that re-implements the fallback inline (à la pre-F291
    /jobs/{id}/description) reopens the same drift class.
    """
    src = _read()
    handler_start = src.find("async def skill_gaps")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "extract_description(" in handler, (
        "F301 regression: skill_gaps no longer falls back to "
        "``extract_description`` for empty text_content rows. "
        "Historical jobs with empty JobDescription contribute zero "
        "again."
    )


def test_skill_gaps_counts_only_rows_with_usable_text():
    """``job_count`` should reflect rows where we actually got
    extractable text, not the raw join cardinality. Otherwise
    ``coverage_pct`` denominator is inflated by rows where
    everything (text_content, raw_json fallback) came up empty.
    """
    src = _read()
    handler_start = src.find("async def skill_gaps")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    # ``job_count = len(rows)`` was the pre-fix anti-pattern. We
    # now ``job_count += 1`` only inside the ``if raw_text:`` branch.
    assert "job_count = 0" in handler, (
        "F301 regression: ``job_count`` no longer initialised to "
        "zero. The denominator math will be wrong."
    )
    assert "job_count += 1" in handler, (
        "F301 regression: ``job_count`` no longer incremented per "
        "usable row. Coverage % will be off."
    )
