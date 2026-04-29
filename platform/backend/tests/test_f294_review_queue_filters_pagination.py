"""F294 — /jobs/review-queue filter + pagination params (closes F230).

F230 found ``GET /jobs/review-queue`` silently dropped every query
param except ``limit``. Two consequences:
  (a) Filter axes (``role_cluster``, ``platform``) were ignored —
      a reviewer who asked for the infra cluster got the unfiltered
      queue.
  (b) Pagination envelope (``page``, ``page_size``, ``total_pages``)
      was hardcoded ``page: 1`` regardless of the query param —
      ``?page=2`` returned the same 20 items as ``?page=1``.

F294 wires up both paths:
  * ``role_cluster: str | None`` and ``platform: str | None`` query
    params; both flow into a shared ``base_filters`` list applied
    to BOTH the items query and the stats aggregation so the
    numbers reconcile.
  * ``page: int = Query(1, ge=1)`` honoured via SQL ``offset((page
    - 1) * limit)``; response echoes the actual page back.
  * ``Job.id.asc()`` secondary sort (F271 stable-pagination
    tiebreaker) so paged traversal doesn't double-count rows
    tied on the primary sort columns.
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
os.environ.setdefault("JWT_SECRET", "pytest-f294")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "jobs.py").read_text()


def test_handler_declares_filter_params():
    """``role_cluster`` and ``platform`` must be declared as query
    params on the handler signature. Pre-fix they were silently
    ignored.
    """
    src = _read()
    handler_start = src.find("async def review_queue")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "role_cluster: str | None" in handler, (
        "F294 regression: ``role_cluster`` filter param was removed "
        "from /jobs/review-queue. Reviewers can't filter by cluster."
    )
    assert "platform: str | None" in handler, (
        "F294 regression: ``platform`` filter param was removed "
        "from /jobs/review-queue."
    )


def test_handler_declares_page_param():
    """``page: int = Query(1, ge=1)`` makes the pagination
    envelope honest.
    """
    src = _read()
    handler_start = src.find("async def review_queue")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "page: int = Query(1, ge=1)" in handler, (
        "F294 regression: ``page`` param removed from "
        "/jobs/review-queue. Pagination envelope is back to lying."
    )


def test_handler_applies_offset_for_pagination():
    """Without ``.offset((page - 1) * limit)`` the ``page`` param
    is just decoration — the SQL still returns the same 20 items.
    """
    src = _read()
    handler_start = src.find("async def review_queue")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert ".offset((page - 1) * limit)" in handler, (
        "F294 regression: ``offset`` is gone — ``page`` query "
        "param doesn't actually advance the result set."
    )


def test_handler_response_echoes_actual_page():
    """The response envelope must echo the ACTUAL ``page`` value,
    not hardcoded 1.
    """
    src = _read()
    handler_start = src.find("async def review_queue")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    # The hardcoded ``"page": 1,`` was the bug. Must be ``"page": page,``
    assert '"page": 1,' not in handler, (
        "F294 regression: response envelope is back to hardcoded "
        "``page: 1``. Clients can't tell which page they're on."
    )
    assert '"page": page' in handler, (
        "F294 regression: response envelope no longer echoes the "
        "operator-supplied ``page`` value."
    )


def test_handler_applies_filters_to_stats_query():
    """The stats-tile counts (today/yesterday/older) must reconcile
    with the items list. Pre-F294 stats used the unfiltered base
    table while the items list (theoretically) could be filtered —
    the F294 filter params would have produced inconsistent stats
    without this shared shape.
    """
    src = _read()
    handler_start = src.find("async def review_queue")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    # ``base_filters`` is the shared list applied to both queries.
    assert "base_filters" in handler, (
        "F294 regression: shared ``base_filters`` list is gone. "
        "Items list and stats can drift under filter scope."
    )
    # Both the main query and the stats query must use the same
    # ``.where(*base_filters)`` shape.
    assert handler.count(".where(*base_filters)") >= 2, (
        "F294 regression: ``base_filters`` no longer applied to "
        "BOTH the items query and the stats query. Counts can "
        "drift under filter scope."
    )


def test_handler_uses_stable_id_tiebreaker():
    """``Job.id.asc()`` as secondary sort prevents page-traversal
    drift on rows tied on (date, my_score, relevance_score). Same
    F271 fix applied to /jobs.
    """
    src = _read()
    handler_start = src.find("async def review_queue")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "Job.id.asc()" in handler, (
        "F294 regression: secondary id-tiebreaker removed from "
        "/jobs/review-queue ORDER BY. Pagination can double-count "
        "rows tied on the primary columns."
    )
