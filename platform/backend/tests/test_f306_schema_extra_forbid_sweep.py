"""F306 — extra="forbid" sweep on remaining Create/Update schemas.

After F305 the platform was down to three schema files still
missing extra="forbid": company_contact.py, credential.py,
discovery.py. F306 closes the sweep so every Pydantic
*Create/*Update body schema rejects unknown keys.

Same F128 defense pattern as F130/F131/F162/F268/F287/F305:
typos like ``frist_name`` instead of ``first_name`` 422 at
parse time instead of silently dropping (which masked admin
keystroke errors as no-op responses).
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
os.environ.setdefault("JWT_SECRET", "pytest-f306")


def test_credential_create_rejects_extra_fields():
    from app.schemas.credential import CredentialCreate
    import pydantic

    # Canonical input passes (need a valid platform Literal value).
    CredentialCreate(
        platform="greenhouse",
        email="x@y.com",
    )
    with pytest.raises(pydantic.ValidationError):
        CredentialCreate(  # type: ignore[call-arg]
            platform="greenhouse",
            email="x@y.com",
            passowrd="typo",
        )


def test_company_contact_create_rejects_extra_fields():
    from app.schemas.company_contact import CompanyContactCreate
    import pydantic

    CompanyContactCreate(first_name="John", email="j@e.com")
    with pytest.raises(pydantic.ValidationError):
        CompanyContactCreate(  # type: ignore[call-arg]
            first_name="John",
            email="j@e.com",
            frist_name="typo",  # F306 catches the typo
        )


def test_company_contact_update_rejects_extra_fields():
    from app.schemas.company_contact import CompanyContactUpdate
    import pydantic

    CompanyContactUpdate(first_name="Updated")
    with pytest.raises(pydantic.ValidationError):
        CompanyContactUpdate(  # type: ignore[call-arg]
            first_name="Updated",
            __evil__="payload",
        )


def test_outreach_update_rejects_extra_fields():
    from app.schemas.company_contact import OutreachUpdate
    import pydantic

    OutreachUpdate(outreach_status="emailed")
    with pytest.raises(pydantic.ValidationError):
        OutreachUpdate(  # type: ignore[call-arg]
            outreach_status="emailed",
            extra_status="typo",
        )


def test_discovered_company_update_rejects_extra_fields():
    from app.schemas.discovery import DiscoveredCompanyUpdate
    import pydantic

    DiscoveredCompanyUpdate(status="added")
    with pytest.raises(pydantic.ValidationError):
        DiscoveredCompanyUpdate(  # type: ignore[call-arg]
            status="added",
            stauts="typo",
        )


def test_discovered_company_update_status_literal():
    """F306 also tightens ``status`` to Literal so non-canonical
    values 422 at parse time."""
    from app.schemas.discovery import DiscoveredCompanyUpdate
    import pydantic

    DiscoveredCompanyUpdate(status="added")
    DiscoveredCompanyUpdate(status="ignored")
    with pytest.raises(pydantic.ValidationError):
        DiscoveredCompanyUpdate(status="bogus_status")
