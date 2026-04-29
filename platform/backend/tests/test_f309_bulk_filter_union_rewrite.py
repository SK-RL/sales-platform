"""F309 — apply F276 UNION-of-IDs rewrite to _build_bulk_filter_query.

F276 rewrote /jobs search to use ``Job.id IN (UNION OF per-column
SELECTs)`` so the F274/F275 trigram indexes can fire (each branch
gets its own SELECT, planner picks the right index per branch).

Same OR-with-EXISTS anti-pattern lived in
``_build_bulk_filter_query`` (the bulk-action filter path). Pre-fix
admin bulk operations on a large catalog (e.g. "bulk-update
everything in the relevant cluster matching 'engineer'") seq-
scanned jobs every time. F309 applies the same rewrite to keep
both search paths consistent.
"""
from __future__ import annotations

import os
import pathlib
import re

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f309")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "jobs.py").read_text()


def _bulk_filter_block(src: str) -> str:
    """Return the body of ``_build_bulk_filter_query``."""
    start = src.find("async def _build_bulk_filter_query")
    assert start > 0, "couldn't locate _build_bulk_filter_query"
    end = src.find("\n@router.", start + 1)
    if end < 0:
        end = src.find("\nasync def ", start + 1)
    body = src[start:end] if end > 0 else src[start:]
    # Strip comments so docstring mentions of OR-EXISTS don't
    # confuse the substring checks.
    return "\n".join(
        ln for ln in body.splitlines()
        if not re.match(r"^\s*#", ln)
    )


def test_bulk_filter_no_longer_uses_or_with_company_has():
    """The OR-with-EXISTS pattern is the bug — it forces seq scan
    on jobs because no single index covers all three branches.
    F309 must replace it.
    """
    src = _read()
    block = _bulk_filter_block(src)
    has_or_with_company_has = (
        "or_(" in block
        and "Job.company.has(" in block
        and "Job.title.ilike" in block
    )
    assert not has_or_with_company_has, (
        "F309 regression: _build_bulk_filter_query is back to the "
        "OR-with-EXISTS shape. ``or_(Job.title.ilike, "
        "Job.company.has(Company.name.ilike), Job.location_raw."
        "ilike)`` forces seq scan on jobs even with the F274/F275 "
        "trigram indexes in place — same anti-pattern F276 fixed "
        "for the main /jobs search path."
    )


def test_bulk_filter_uses_union_of_ids():
    """The fix shape is three independent ``select(Job.id)``
    sub-selects unioned together, then ``Job.id.in_(...)`` filters
    the outer query. Lets each branch pick its own index.
    """
    src = _read()
    block = _bulk_filter_block(src)
    assert "Job.title.ilike" in block, (
        "F309 regression: bulk-filter title-branch SELECT missing."
    )
    assert "Company.name.ilike" in block, (
        "F309 regression: bulk-filter company-branch SELECT missing."
    )
    assert "Job.location_raw.ilike" in block, (
        "F309 regression: bulk-filter location-branch SELECT "
        "missing."
    )
    assert ".union(" in block, (
        "F309 regression: bulk-filter no longer uses ``.union(...)``"
        " to combine per-column SELECTs."
    )
    assert "Job.id.in_(" in block, (
        "F309 regression: bulk-filter no longer applies the UNION "
        "result via ``Job.id.in_(...)``."
    )


def test_bulk_filter_company_join_inside_subquery():
    """Same defense as F276 — the Company join must live ONLY
    inside the company-branch sub-select, not on the outer query.
    """
    src = _read()
    block = _bulk_filter_block(src)
    join_company_count = block.count(".join(Company,")
    assert join_company_count == 1, (
        f"F309 regression: expected exactly 1 ``.join(Company, ...)``"
        f" call (inside the company-match subquery only); found "
        f"{join_company_count}."
    )
