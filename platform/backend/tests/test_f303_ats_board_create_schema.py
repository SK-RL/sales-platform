"""F303 — harden ``ATSBoardCreate`` schema.

Pre-fix the schema was bare ``platform: str`` + ``slug: str`` with
no length cap, no ``extra="forbid"``, no per-field validation. F128
pattern: typos like ``platfrm`` silently dropped, 5KB strings
crashed the underlying ``String(N)`` writer with HTTP 500. The
handler in ``companies.py`` validates platform against
``FETCHER_MAP`` at runtime, but that fires AFTER schema parsing.
F303 adds the schema-layer constraints so malformed input 422s
at parse time.
"""
from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f303")


def test_extra_forbid():
    from app.schemas.company import ATSBoardCreate
    import pydantic

    # Canonical input passes.
    ATSBoardCreate(platform="greenhouse", slug="acme")
    # Extra field rejected.
    with pytest.raises(pydantic.ValidationError):
        ATSBoardCreate(  # type: ignore[call-arg]
            platform="greenhouse",
            slug="acme",
            is_admin_override=True,
        )


def test_platform_length_cap():
    from app.schemas.company import ATSBoardCreate
    import pydantic

    # 50 chars passes
    ATSBoardCreate(platform="x" * 50, slug="acme")
    # 51 rejected
    with pytest.raises(pydantic.ValidationError):
        ATSBoardCreate(platform="x" * 51, slug="acme")
    # Empty rejected
    with pytest.raises(pydantic.ValidationError):
        ATSBoardCreate(platform="", slug="acme")


def test_slug_length_cap():
    from app.schemas.company import ATSBoardCreate
    import pydantic

    ATSBoardCreate(platform="greenhouse", slug="x" * 200)
    with pytest.raises(pydantic.ValidationError):
        ATSBoardCreate(platform="greenhouse", slug="x" * 201)
    with pytest.raises(pydantic.ValidationError):
        ATSBoardCreate(platform="greenhouse", slug="")


def test_is_active_default_true():
    from app.schemas.company import ATSBoardCreate

    a = ATSBoardCreate(platform="greenhouse", slug="acme")
    assert a.is_active is True


# Re-export pytest as a top-level so the assertions use it.
import pytest  # noqa: E402
