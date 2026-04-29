"""F304 — HTML-strip Company text fields.

Pre-fix ``CompanyCreate`` / ``CompanyUpdate`` accepted ``name``,
``description``, ``industry``, ``headquarters`` with length caps
but no HTML sanitization, so an admin payload like
``description="<script>alert(1)</script>About us…"`` could be
persisted and rendered verbatim in the company UI. Same vector
as F162/F285/F287/F289.

F304 routes those four fields through ``strip_html_tags`` on both
create and update paths.
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
os.environ.setdefault("JWT_SECRET", "pytest-f304")


def test_create_strips_html_in_text_fields():
    from app.schemas.company import CompanyCreate

    c = CompanyCreate(
        name="<script>alert(1)</script>Acme",
        slug="acme",
        industry="<img src=x onerror=alert(1)>SaaS",
        headquarters="<svg onload=alert(1)>SF",
        description="<a href='javascript:'>About us</a>",
    )
    for field, value in (
        ("name", c.name),
        ("industry", c.industry),
        ("headquarters", c.headquarters),
        ("description", c.description),
    ):
        assert "<" not in value, (
            f"F304 regression: CompanyCreate.{field} no longer "
            f"strips HTML. Got: {value!r}"
        )
        assert "alert" not in value, (
            f"F304 regression: {field} still contains script "
            f"payload."
        )


def test_update_strips_html_in_text_fields():
    """PATCH path was a parallel vector pre-fix."""
    from app.schemas.company import CompanyUpdate

    u = CompanyUpdate(
        name="<script>x</script>Renamed",
        description="<img src=x onerror=alert(1)>Description",
    )
    assert u.name and "<" not in u.name
    assert u.description and "<" not in u.description


def test_safe_strings_pass_through_unchanged():
    """No false positives — normal company names round-trip."""
    from app.schemas.company import CompanyCreate

    c = CompanyCreate(
        name="Acme Corporation",
        slug="acme",
        industry="Software",
        headquarters="San Francisco, CA",
        description="A leading SaaS provider.",
    )
    assert c.name == "Acme Corporation"
    assert c.industry == "Software"
    assert c.headquarters == "San Francisco, CA"
    assert c.description == "A leading SaaS provider."
