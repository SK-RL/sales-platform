"""F299 — /companies/{id}/jobs canonical pagination envelope (closes F212).

F212 found three different pagination envelope shapes inside the
``companies`` router. ``GET /companies`` used the canonical
``{items, total, page, page_size, total_pages}``;
``GET /companies/{id}/jobs`` used ``{items, total, page, per_page,
pages}`` (the F108 drifted shape); contacts + scores returned only
``{items}``. Frontends keyed on the canonical names rendered "page
1 of 1" on /companies/{id}/jobs because ``total_pages`` was
undefined.

F299 normalizes /companies/{id}/jobs to the canonical envelope.
The query param name ``per_page`` stays unchanged for backwards
compatibility with existing callers; only response keys flip.
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
os.environ.setdefault("JWT_SECRET", "pytest-f299")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_company_jobs_returns_page_size_not_per_page():
    """The response envelope must use ``page_size`` (canonical),
    not ``per_page`` (drifted shape)."""
    src = (_BACKEND / "app" / "api" / "v1" / "companies.py").read_text()
    handler_start = src.find("async def company_jobs")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    # Find the return statement.
    return_start = handler.rfind("return {")
    return_block = handler[return_start:return_start + 400]
    assert '"page_size":' in return_block, (
        "F299 regression: /companies/{id}/jobs envelope no longer "
        "emits the canonical ``page_size`` key. Frontends keyed on "
        "page_size will render incorrect pager state again."
    )
    assert '"total_pages":' in return_block, (
        "F299 regression: /companies/{id}/jobs envelope no longer "
        "emits the canonical ``total_pages`` key."
    )
    # The drifted ``per_page`` / ``pages`` keys must NOT appear in
    # the return dict (they're the bug).
    assert '"per_page":' not in return_block, (
        "F299 regression: response envelope reverted to the "
        "drifted ``per_page`` key — F212 reopens."
    )
    assert '"pages":' not in return_block, (
        "F299 regression: response envelope reverted to the "
        "drifted ``pages`` key — F212 reopens."
    )


def test_query_param_per_page_unchanged_for_back_compat():
    """The QUERY PARAM stays ``per_page`` so existing callers
    don't break. Only the RESPONSE keys flip.
    """
    src = (_BACKEND / "app" / "api" / "v1" / "companies.py").read_text()
    handler_start = src.find("async def company_jobs")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    sig_end = src.find("):\n", handler_start)
    sig = src[handler_start:sig_end]
    # F299 + F319 — back-compat means EITHER form is acceptable
    # (the F319 dual-param shape ``per_page: int | None = Query(
    # default=None, deprecated=True)`` still accepts ``?per_page=``
    # from existing clients):
    assert (
        "per_page: int = Query(" in sig
        or "per_page: int | None = Query(" in sig
    ), (
        "F299 regression: ``per_page`` query param was renamed "
        "or removed entirely. Existing callers will get 422 on "
        "the missing param. F319's dual-param shape is also "
        "acceptable — back-compat is preserved either way."
    )
