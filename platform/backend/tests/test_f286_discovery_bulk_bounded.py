"""F286 — discovery bulk-import/bulk-ignore: UUID typing + length cap
+ batched IN query (closes F137 a+b).

F137(a): ``BulkIdsRequest.ids: list[str]`` accepted non-UUID strings
that bubbled ``psycopg.DataError`` as opaque HTTP 500. F126 pattern
recurrence — F286 retypes to ``list[UUID]`` so Pydantic 422s at parse
time.

F137(b): unbounded list × per-id SELECT loop produced an N-round-trip
DoS surface. 1000 IDs ≈ 1000 SELECTs; 100k IDs would hold an admin
DB connection for minutes. F286 caps the list at 200 elements via
``Field(max_length=BULK_IDS_MAX)`` AND replaces the per-id loop with
a single ``WHERE id.in_(body.ids)`` batched fetch. Bulk-ignore now
runs ONE batched ``UPDATE ... WHERE id IN (...) AND status != 'ignored'``
instead of an N-iteration loop.
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
os.environ.setdefault("JWT_SECRET", "pytest-f286")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "discovery.py").read_text()


def test_bulk_ids_request_uses_uuid_typing():
    """``ids`` must be ``list[UUID]`` so non-UUID strings 422 at
    parse time instead of bubbling DataError as HTTP 500.
    """
    src = _read()
    assert "ids: list[UUID]" in src, (
        "F286 regression: ``BulkIdsRequest.ids`` is no longer "
        "typed as ``list[UUID]``. Non-UUID strings will reopen the "
        "F126 / F141 / F181 500-instead-of-422 surface."
    )


def test_bulk_ids_request_caps_length():
    """``Field(max_length=BULK_IDS_MAX)`` caps the DoS surface.
    Without it, 100k IDs could pin an admin DB connection for
    minutes (even with the IN-batch refactor in place — Postgres
    has its own param-array ceiling).
    """
    src = _read()
    assert "BULK_IDS_MAX" in src, (
        "F286 regression: ``BULK_IDS_MAX`` constant removed. "
        "Without a documented cap, future contributors may bump "
        "the schema limit blindly."
    )
    assert "max_length=BULK_IDS_MAX" in src or "max_length=200" in src, (
        "F286 regression: ``BulkIdsRequest.ids`` no longer caps "
        "list length. Unbounded input reopens the DoS surface."
    )


def test_bulk_import_uses_batched_in_query():
    """The handler must use ``id.in_(body.ids)`` for the candidate
    fetch, NOT a per-id loop with ``id == dc_id``. Per-id is the
    N+1 anti-pattern that F137 documented.
    """
    src = _read()
    handler_start = src.find("async def bulk_import_discovered")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "DiscoveredCompany.id.in_(" in handler, (
        "F286 regression: bulk_import_discovered no longer uses a "
        "batched ``id.in_(...)`` query. The N+1 round-trip pattern "
        "is back."
    )
    # Per-id SELECT inside the loop is the bug — must NOT exist.
    # Accept the IN-clause SELECT but fail if there's a SELECT in
    # a ``for`` body that filters by a single id.
    forbidden = "select(DiscoveredCompany).where(DiscoveredCompany.id == dc_id)"
    assert forbidden not in handler, (
        "F286 regression: per-id SELECT loop is back — same N+1 "
        "anti-pattern F137 documented."
    )


def test_bulk_import_pre_checks_existing_slugs_in_one_query():
    """Pre-fix the slug-already-exists check ran one SELECT per
    candidate. Now must be a single batched ``Company.slug.in_(...)``
    query before the loop.
    """
    src = _read()
    handler_start = src.find("async def bulk_import_discovered")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "Company.slug.in_(" in handler, (
        "F286 regression: bulk_import_discovered no longer batches "
        "the slug-exists pre-check. N round-trips per candidate "
        "is back."
    )


def test_bulk_ignore_uses_single_batched_update():
    """Pre-fix the handler ran SELECT-then-mutate per id. Must now
    run a single ``UPDATE ... WHERE id IN (...) AND status != 'ignored'``
    that returns rowcount.
    """
    src = _read()
    handler_start = src.find("async def bulk_ignore_discovered")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end] if handler_end > 0 else src[handler_start:]
    # Single UPDATE call, with id.in_ filter
    assert "DiscoveredCompany.id.in_(body.ids)" in handler, (
        "F286 regression: bulk_ignore_discovered no longer uses a "
        "batched IN clause."
    )
    # Should NOT have a per-id select loop
    assert (
        "select(DiscoveredCompany).where(DiscoveredCompany.id == dc_id)"
        not in handler
    ), (
        "F286 regression: bulk_ignore_discovered's per-id SELECT "
        "loop is back."
    )
    # Should reference rowcount (the result of the batched update)
    assert "rowcount" in handler, (
        "F286 regression: bulk_ignore_discovered no longer reads "
        "``result.rowcount`` from the batched UPDATE."
    )
