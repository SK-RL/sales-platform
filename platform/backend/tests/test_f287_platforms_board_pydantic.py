"""F287 — POST /platforms/boards Pydantic schema (closes F147 a-d).

F147 found the handler took ``body: dict`` directly with
``body.get(...)`` field extraction. Consequences:
  (a) ``extra="forbid"`` couldn't fire — extra fields silently
      dropped (F128 pattern).
  (b) ``company_name`` had no length cap — 5KB names crashed the
      Postgres ``String(N)`` column writer with HTTP 500.
  (c) HTML in ``company_name`` rendered verbatim in the admin UI
      (stored XSS — same vector as F162 / F148 / F149).
  (d) Slug had no validation.

F287 reshapes the input as ``_BoardCreateBody`` Pydantic model
with ``extra="forbid"``, length caps, and ``strip_html_tags`` on
``company_name``.
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
os.environ.setdefault("JWT_SECRET", "pytest-f287")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "platforms.py").read_text()


def test_board_create_body_class_exists():
    """The handler must accept a Pydantic model, NOT a bare dict.
    ``body: dict`` was the F147 root cause — re-introducing it
    reopens all four sub-issues simultaneously.
    """
    src = _read()
    assert "class _BoardCreateBody" in src or "class BoardCreate" in src, (
        "F287 regression: ``_BoardCreateBody`` schema was removed. "
        "If the handler is back to ``body: dict``, F147 a-d are "
        "all reopen."
    )


def test_handler_signature_uses_pydantic_body_not_dict():
    """The ``add_board`` handler signature must declare
    ``body: _BoardCreateBody`` (or whatever the schema is named),
    NOT ``body: dict``.
    """
    src = _read()
    handler_start = src.find("async def add_board")
    handler_end = src.find("@router.", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    handler = src[handler_start:handler_end]
    assert "body: dict" not in handler, (
        "F287 regression: ``add_board`` is back to ``body: dict``. "
        "Pydantic validation is bypassed; F147 a-d are reopen."
    )
    assert "body: _BoardCreateBody" in handler or "body: BoardCreate" in handler, (
        "F287 regression: ``add_board`` no longer takes a Pydantic "
        "model body."
    )


def test_schema_uses_extra_forbid():
    """``extra="forbid"`` so unknown fields produce a clean 422
    instead of silently dropping (F128 pattern).
    """
    src = _read()
    # Slice to the schema class so we don't accidentally pick up
    # ``extra="forbid"`` from another schema in the same file.
    schema_start = src.find("class _BoardCreateBody")
    if schema_start < 0:
        schema_start = src.find("class BoardCreate")
    schema_end = src.find("@router.", schema_start + 1)
    if schema_end < 0:
        # Walk to the next class definition (or EOF) — we just need
        # something narrower than the whole file.
        schema_end = src.find("\nclass ", schema_start + 10)
        if schema_end < 0:
            schema_end = len(src)
    schema = src[schema_start:schema_end]
    assert 'extra="forbid"' in schema, (
        "F287 regression: ``_BoardCreateBody`` no longer enforces "
        "``extra=\"forbid\"``. Unknown fields silently drop instead "
        "of returning a 422."
    )


def test_schema_caps_company_name_length():
    """Without a ``max_length`` cap on ``company_name``, a 5KB
    payload crashes the underlying ``String(N)`` column writer
    with HTTP 500 (F147(b)).
    """
    src = _read()
    schema_start = src.find("class _BoardCreateBody")
    schema_end = src.find("@router.", schema_start + 1)
    if schema_end < 0:
        schema_end = src.find("\nclass ", schema_start + 10)
    schema = src[schema_start:schema_end] if schema_end > 0 else src[schema_start:]
    # ``company_name`` must have a max_length cap. Don't pin exact
    # value so future tuning isn't blocked.
    assert "company_name" in schema
    assert "max_length=" in schema, (
        "F287 regression: ``_BoardCreateBody`` no longer caps the "
        "company_name length. 5KB payloads crash the DB writer."
    )


def test_schema_strips_html_in_company_name():
    """``company_name`` must route through ``strip_html_tags`` so
    a ``<script>alert(1)</script>`` payload can't be persisted.
    Same defense feedback (F162), role-cluster display_name (F285)
    apply.
    """
    src = _read()
    schema_start = src.find("class _BoardCreateBody")
    schema_end = src.find("@router.", schema_start + 1)
    schema = src[schema_start:schema_end] if schema_end > 0 else src[schema_start:]
    assert "strip_html_tags(" in schema, (
        "F287 regression: ``_BoardCreateBody.company_name`` no "
        "longer strips HTML. Stored XSS via the admin UI returns."
    )
