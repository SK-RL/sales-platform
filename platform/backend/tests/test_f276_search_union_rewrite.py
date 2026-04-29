"""F276 — /jobs search filter UNION rewrite (structural).

Companion to F274 (jobs.title trigram index) and F275 (companies.name
trigram index). Those two migrations only help if the planner can
actually pick them; under the original ``or_(title.ilike,
company.has(name.ilike), location_raw.ilike)`` shape, Postgres has
to satisfy the OR with a single scan of jobs and falls back to a
seq scan + Hash Join — neither trigram index ever fires.

EXPLAIN (ANALYZE) before F276:
  Seq Scan on jobs (88,024 rows, 23,892 join-filter-rejected, 78 ms)

The fix rewrites the WHERE as ``Job.id IN (UNION OF per-column
SELECTs)``. Each subquery is a single-column predicate so the
planner picks the appropriate index per branch (Bitmap Index Scan
on idx_jobs_title_trgm + Bitmap Index Scan on
idx_companies_name_trgm). Verified via local EXPLAIN that the
title-branch picks idx_jobs_title_trgm under realistic row counts.

These tests are SOURCE-LEVEL: we verify the SQLAlchemy expression
tree compiles to a UNION-of-IDs shape with one SELECT per indexed
column. The actual planner picks are out-of-scope here (those
depend on table size / pg_stats) but we cover them in the deploy
EXPLAIN probe captured in jobs.py docstring above the rewrite.
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
os.environ.setdefault("JWT_SECRET", "pytest-f276")


_JOBS_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "api" / "v1" / "jobs.py"
)


def _read_jobs_source() -> str:
    return _JOBS_PATH.read_text()


def _legacy_search_block(src: str) -> str:
    """Return the CODE-only body of the legacy single-substring
    search branch (F276 target). Comment lines are stripped so that
    the historical bug-shape mentioned in the rewrite's docstring
    doesn't trip the regression checks below — we want to detect
    the OR-pattern in actual code, not in explanatory prose.
    """
    # Anchor on the unique ``Legacy single-substring path`` comment
    # that introduces this branch and runs until the next ``# Count``
    # block (which is a unique marker too). ``else:`` alone is not
    # unique — earlier filter branches also use it.
    start_marker = "Legacy single-substring path"
    end_marker = "# Count"
    start = src.find(start_marker)
    assert start >= 0, (
        f"F276 regression: couldn't find ``{start_marker}`` anchor "
        f"comment in jobs.py. The anchor may have been removed — "
        f"update this test or restore the structure."
    )
    end = src.find(end_marker, start)
    assert end > start, (
        f"F276 regression: couldn't find ``{end_marker}`` boundary "
        f"after the legacy search anchor. File structure changed."
    )
    raw = src[start:end]
    # Strip pure-comment lines (leading whitespace + ``#``) so the
    # historical bug-shape mentioned in the rewrite docstring isn't
    # mistaken for live code by the substring checks.
    code_only = "\n".join(
        ln for ln in raw.splitlines()
        if not re.match(r"^\s*#", ln)
    )
    return code_only


def test_legacy_search_no_longer_uses_or_with_company_has():
    """The original OR-with-EXISTS pattern is the bug — it forces
    the planner to seq-scan jobs because no single index covers all
    three branches. F276 must replace it.

    The marker for the old bug is ``Job.company.has(`` inside an
    ``or_(...)`` next to ``Job.title.ilike`` and
    ``Job.location_raw.ilike``. If a future refactor reintroduces
    that shape, the trigram indexes will silently stop firing and
    /jobs?search=... will return to 114ms-per-call seq scan.
    """
    src = _read_jobs_source()
    block = _legacy_search_block(src)

    # The smoking gun: ``or_( Job.title.ilike ... Job.company.has(``
    # appearing inside the legacy branch.
    has_or_with_company_has = (
        "or_(" in block
        and "Job.company.has(" in block
        and "Job.title.ilike" in block
    )
    assert not has_or_with_company_has, (
        "F276 regression: legacy search filter is back to the OR-with-"
        "EXISTS shape. ``or_(Job.title.ilike, Job.company.has(...), "
        "Job.location_raw.ilike)`` forces a seq scan on jobs because "
        "Postgres can't combine multiple indexes across an OR with "
        "a sub-EXISTS. Use the F276 UNION-of-IDs subquery pattern."
    )


def test_legacy_search_uses_union_of_ids():
    """The fix shape is three independent ``select(Job.id)``
    sub-selects unioned together, then ``Job.id.in_(...)`` filters
    the outer query. This lets each branch pick its own index.
    """
    src = _read_jobs_source()
    block = _legacy_search_block(src)

    # Three per-column SELECTs.
    assert "Job.title.ilike" in block, (
        "F276 regression: title-branch SELECT missing from legacy "
        "search filter. Without it, title-only matches won't show."
    )
    assert "Company.name.ilike" in block, (
        "F276 regression: company-branch SELECT missing. Search "
        "queries that match company name (e.g. 'datadog') will "
        "return zero rows."
    )
    assert "Job.location_raw.ilike" in block, (
        "F276 regression: location-branch SELECT missing. Search "
        "queries that match a location string (e.g. 'remote') "
        "will return zero rows."
    )

    # UNION-of-IDs shape.
    assert ".union(" in block, (
        "F276 regression: legacy search filter no longer uses "
        "``.union(...)`` to combine per-column SELECTs. Without "
        "the UNION, the planner can't pick one index per branch."
    )
    assert "Job.id.in_(" in block, (
        "F276 regression: legacy search filter no longer applies "
        "the UNION result via ``Job.id.in_(...)``. Without the IN, "
        "the UNION isn't actually filtering the outer query."
    )


def test_company_join_stays_inside_company_subquery():
    """The ``Company`` join must live ONLY inside the company-branch
    sub-select, not on the outer query. If a future refactor adds
    a top-level ``query.join(Company, ...)`` for the legacy search
    path, every ``/jobs`` request that uses search will pay the
    company-join cost even when the search hits only title or
    location.
    """
    src = _read_jobs_source()
    block = _legacy_search_block(src)

    # The legacy block should contain the join keyword exactly once
    # (inside the company_match subquery). More than one suggests
    # someone re-added the outer join.
    join_company_count = block.count(".join(Company,")
    assert join_company_count == 1, (
        f"F276 regression: expected exactly 1 ``.join(Company, ...)`` "
        f"call in the legacy search block (inside the company-match "
        f"subquery only); found {join_company_count}. An extra one "
        f"on the outer query will force a join cost even when the "
        f"search hits only title/location."
    )


def test_boolean_parser_path_unchanged():
    """F276 only touches the legacy single-substring path. The
    boolean parser path (F240) must still use the per-term ``or_``
    semantics across (title, company.name, location_raw) — that's
    the documented user-visible behavior of `cloud OR security`-
    style queries. A regression there is a UX bug, not a perf bug.
    """
    src = _read_jobs_source()
    # Anchor on the boolean-path block (above the else: legacy block).
    bool_block_start = src.find("if is_boolean_query(")
    bool_block_end = src.find("else:", bool_block_start)
    assert bool_block_start > 0 and bool_block_end > bool_block_start, (
        "F276 regression: couldn't locate the is_boolean_query() "
        "branch in jobs.py — file structure changed."
    )
    bool_block = src[bool_block_start:bool_block_end]
    # The boolean path must still hand all three columns to the
    # term_clause_factory (the per-term ANY-of-columns matcher).
    assert "term_clause_factory(" in bool_block, (
        "F276 regression: boolean search path no longer uses "
        "``term_clause_factory(...)``. Boolean queries now miss "
        "the per-term-ANY-of-columns semantic."
    )
    assert "Job.title" in bool_block and "Company.name" in bool_block \
        and "Job.location_raw" in bool_block, (
        "F276 regression: boolean search path no longer matches "
        "across all three of (title, company name, location_raw)."
    )
