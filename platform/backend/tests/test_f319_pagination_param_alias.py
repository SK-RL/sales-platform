"""F319 — accept ``page_size`` alias on legacy ``per_page`` endpoints (closes F108 query-param drift).

F108 / F212 / F299 closed the response-side drift (every paginated
endpoint now returns ``{items, total, page, page_size, total_pages}``).
The remaining drift was on the INPUT side: 8 endpoints required
``?per_page=`` while everywhere else accepts ``?page_size=``.

F319 declares BOTH params on each endpoint. Either one works;
when both are supplied the legacy ``per_page`` wins (back-compat
— existing callers keep their behaviour). New callers should use
``page_size`` to match every other paginated endpoint in the API.

Tests verify each affected endpoint declares the dual-param
shape (``per_page: int | None`` deprecated + ``page_size: int``
canonical) so the F108 input-side drift can't silently
re-emerge.
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
os.environ.setdefault("JWT_SECRET", "pytest-f319")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _slice_handler(src: str, name: str) -> str:
    start = src.find(f"async def {name}(")
    if start < 0:
        # No async — try sync
        start = src.find(f"def {name}(")
    if start < 0:
        return ""
    end = src.find("\n@router.", start + 1)
    if end < 0:
        end = len(src)
    return src[start:end]


_AFFECTED = [
    ("api/v1/reviews.py", "list_reviews"),
    ("api/v1/discovery.py", "list_runs"),
    ("api/v1/discovery.py", "list_discovered_companies"),
    ("api/v1/jobs.py", "list_jobs"),
    ("api/v1/rules.py", "list_rules"),
    ("api/v1/career_pages.py", "list_career_pages"),
    ("api/v1/companies.py", "list_companies"),
    ("api/v1/companies.py", "company_jobs"),
]


def test_each_affected_endpoint_accepts_dual_params():
    """Each pre-F319 ``per_page``-only endpoint must now declare
    BOTH ``per_page`` (deprecated) and ``page_size`` (canonical)
    so a caller using either name gets a working response.
    """
    misses: list[str] = []
    for rel_path, handler in _AFFECTED:
        full = _BACKEND / "app" / rel_path
        try:
            src = full.read_text()
        except FileNotFoundError:
            misses.append(f"{rel_path} missing")
            continue
        body = _slice_handler(src, handler)
        if not body:
            misses.append(f"{rel_path}::{handler} not found")
            continue
        # Must accept both names. ``per_page`` deprecated, ``page_size`` canonical.
        if "per_page" not in body or "page_size" not in body:
            misses.append(
                f"{rel_path}::{handler} missing per_page+page_size pair"
            )
            continue
        # Must include the resolution line so the handler picks the
        # legacy value when supplied (back-compat) else falls back
        # to canonical.
        if "per_page if per_page is not None else page_size" not in body:
            misses.append(
                f"{rel_path}::{handler} missing dual-param resolution"
            )
    assert not misses, (
        "F319 regression: dual-param shape missing on the "
        f"following endpoints: {misses}"
    )


def test_per_page_marked_deprecated():
    """The legacy ``per_page`` should be marked ``deprecated=True``
    so the OpenAPI schema flags it for clients (and so future
    `--strict` linting can drop it cleanly when ready).
    """
    misses: list[str] = []
    for rel_path, handler in _AFFECTED:
        full = _BACKEND / "app" / rel_path
        src = full.read_text()
        body = _slice_handler(src, handler)
        if "per_page" not in body:
            continue
        # Look for the deprecation flag near the per_page declaration.
        per_page_idx = body.find("per_page:")
        if per_page_idx < 0:
            continue
        # Slice the param declaration (next ``,`` after the type).
        decl_window = body[per_page_idx:per_page_idx + 200]
        if "deprecated=True" not in decl_window:
            misses.append(f"{rel_path}::{handler}")
    assert not misses, (
        f"F319 regression: ``per_page`` not marked "
        f"``deprecated=True`` on: {misses}. OpenAPI schema "
        f"won't flag the legacy param for migration-aware "
        f"clients."
    )
