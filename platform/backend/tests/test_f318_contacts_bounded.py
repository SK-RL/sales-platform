"""F318 — bound /companies/{id}/contacts response.

Pre-fix the handler returned ALL contacts unbounded with the
naked ``{"items": [...]}`` envelope. A company with 5k contacts
would dump ~1.5 MB JSON in one response; same DoS class as F107
(export). Frontend pagination components expecting the canonical
``page_size``/``total_pages`` shape rendered "page 1 of 1"
because the fields were undefined.

F318:
  * ``page`` + ``page_size`` Query params, capped at 1000
  * Canonical envelope ``{items, total, page, page_size,
    total_pages}`` matching every other paginated list endpoint
    in the API
  * F271-style stable id tiebreaker on ORDER BY so paged
    traversal doesn't shift rows between pages
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
os.environ.setdefault("JWT_SECRET", "pytest-f318")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "companies.py").read_text()


def test_handler_accepts_page_and_page_size():
    src = _read()
    handler_start = src.find("async def list_contacts")
    handler_end = src.find("@router.", handler_start + 1)
    sig = src[handler_start:handler_end]
    assert "page: int = Query(" in sig, (
        "F318 regression: list_contacts no longer accepts ``page``."
    )
    assert "page_size: int = Query(" in sig, (
        "F318 regression: list_contacts no longer accepts "
        "``page_size``. Unbounded response surface returns."
    )


def test_handler_caps_page_size():
    src = _read()
    handler_start = src.find("async def list_contacts")
    handler_end = src.find("@router.", handler_start + 1)
    sig = src[handler_start:handler_end]
    # Cap exists (le= or max_length pattern). Don't pin exact value
    # so future tuning isn't blocked.
    assert "le=" in sig, (
        "F318 regression: page_size has no upper bound — DoS "
        "surface (5k+ contact list) returns."
    )


def test_handler_emits_canonical_envelope():
    src = _read()
    handler_start = src.find("async def list_contacts")
    handler_end = src.find("@router.", handler_start + 1)
    body = src[handler_start:handler_end]
    return_idx = body.rfind("return {")
    return_block = body[return_idx:return_idx + 400]
    for key in ('"items":', '"total":', '"page":', '"page_size":', '"total_pages":'):
        assert key in return_block, (
            f"F318 regression: list_contacts envelope missing "
            f"canonical key {key!r}. Frontend pagination "
            f"components break."
        )


def test_order_by_includes_stable_tiebreaker():
    """F271-class stability: secondary id-asc tiebreaker so
    paged traversal doesn't double-count rows tied on the
    primary sort columns (seniority, last_name).
    """
    src = _read()
    handler_start = src.find("async def list_contacts")
    handler_end = src.find("@router.", handler_start + 1)
    body = src[handler_start:handler_end]
    assert "CompanyContact.id.asc()" in body, (
        "F318 regression: order_by lost the F271-style id "
        "tiebreaker. Pagination can shift rows between pages "
        "when ties on (seniority, last_name)."
    )
