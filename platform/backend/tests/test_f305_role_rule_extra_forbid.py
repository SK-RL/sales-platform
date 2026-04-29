"""F305 — extra="forbid" on RoleRuleCreate / RoleRuleUpdate.

Pre-fix the rule schemas had length caps + non-empty checks but
no ``extra="forbid"``. Admin-side typos like ``custer`` instead
of ``cluster`` silently dropped, masking keystroke errors as
no-op responses. Same F128 pattern as F130 (review), F131
(company), F162 (feedback), F268 (role cluster), F287 (board).
"""
from __future__ import annotations

import os
import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f305")


def test_create_rejects_extra_fields():
    from app.schemas.rule import RoleRuleCreate
    import pydantic

    # Canonical input passes.
    RoleRuleCreate(cluster="infra", base_role="DevOps", keywords=["devops"])
    # Extra field rejected.
    with pytest.raises(pydantic.ValidationError):
        RoleRuleCreate(  # type: ignore[call-arg]
            cluster="infra",
            base_role="DevOps",
            keywords=["devops"],
            custer="typo",  # F305 — would silently drop pre-fix
        )


def test_update_rejects_extra_fields():
    from app.schemas.rule import RoleRuleUpdate
    import pydantic

    RoleRuleUpdate(cluster="infra")
    with pytest.raises(pydantic.ValidationError):
        RoleRuleUpdate(  # type: ignore[call-arg]
            cluster="infra",
            __evil__="payload",
        )


def test_existing_validation_still_in_place():
    """F305 didn't relax F128 — empty keywords still rejected."""
    from app.schemas.rule import RoleRuleCreate
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        RoleRuleCreate(cluster="infra", base_role="DevOps", keywords=[])
