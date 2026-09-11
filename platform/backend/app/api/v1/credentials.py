"""Platform credential management endpoints.

Regression findings 77, 78, 79 all landed in the same rewrite:
  - #77 (HIGH): `profile_url` previously accepted `javascript:` schemes
    and the frontend rendered them as `<a href>`, enabling stored XSS.
    Fixed by moving to Pydantic `CredentialCreate` whose validator
    rejects any scheme other than http/https/relative.
  - #78 (MEDIUM): DELETE did not delete — it mangled the email with an
    `"archived_"` prefix and blanked the password, then the row kept
    showing up in GET responses. GDPR Art.17 non-compliant. Now does
    an actual `await db.delete(cred)`.
  - #79 (INFO): `body: dict` dropped validation, type coercion, and
    OpenAPI schema. Replaced with `body: CredentialCreate`.
"""

import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.platform_credential import PlatformCredential
from app.models.resume import Resume
from app.models.user import User
from app.api.deps import get_current_user
from app.schemas.credential import CredentialCreate
from app.utils.crypto import encrypt_credential

router = APIRouter(prefix="/credentials", tags=["credentials"])

# Keep in lockstep with `schemas/credential.SUPPORTED_PLATFORM_LITERALS`.
# `linkedin` is here for profile-URL attachment, not ATS integration —
# see the long-form comment on the Literal in schemas/credential.py.
SUPPORTED_PLATFORMS = [
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters",
    "recruitee", "breezy", "personio", "rippling", "jazzhr", "teamtailor", "pinpoint", "hireology", "dover", "gem", "zoho", "bamboohr", "jobvite", "wellfound", "himalayas",
    "linkedin",
]


@router.get("/{resume_id}")
async def list_credentials(
    resume_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List credentials for a resume (passwords masked)."""
    # Verify resume ownership
    resume = (await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )).scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    result = await db.execute(
        select(PlatformCredential)
        .where(PlatformCredential.resume_id == resume.id)
        .order_by(PlatformCredential.platform)
    )
    creds = result.scalars().all()

    return {
        "items": [
            {
                "id": str(c.id),
                "resume_id": str(c.resume_id),
                "platform": c.platform,
                "email": c.email,
                "has_password": bool(c.encrypted_password),
                "profile_url": c.profile_url,
                "is_verified": c.is_verified,
                "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
                "created_at": c.created_at.isoformat(),
            }
            for c in creds
        ],
        "supported_platforms": SUPPORTED_PLATFORMS,
    }


@router.post("/{resume_id}")
async def save_credential(
    resume_id: UUID,
    body: CredentialCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add or update a credential for a platform."""
    resume = (await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )).scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    # CredentialCreate has already validated: platform is in the
    # allowlist, email is a real address, profile_url has a safe scheme
    # (or is empty), password fits the column size cap.
    platform = body.platform
    email = body.email.strip().lower()
    password = body.password or ""
    profile_url = (body.profile_url or "").strip()

    # Check if credential already exists for this resume+platform
    existing = (await db.execute(
        select(PlatformCredential).where(
            PlatformCredential.resume_id == resume.id,
            PlatformCredential.platform == platform,
        )
    )).scalar_one_or_none()

    if existing:
        existing.email = email
        if password:
            existing.encrypted_password = encrypt_credential(password)
        if profile_url:
            existing.profile_url = profile_url
        existing.is_verified = False  # Reset verification on update
        db.add(existing)
        cred = existing
    else:
        cred = PlatformCredential(
            id=uuid.uuid4(),
            resume_id=resume.id,
            platform=platform,
            email=email,
            encrypted_password=encrypt_credential(password) if password else "",
            profile_url=profile_url,
        )
        db.add(cred)

    await db.commit()
    await db.refresh(cred)

    return {
        "id": str(cred.id),
        "platform": cred.platform,
        "email": cred.email,
        "has_password": bool(cred.encrypted_password),
        "profile_url": cred.profile_url,
        "is_verified": cred.is_verified,
    }


@router.delete("/{resume_id}/{platform}")
async def delete_credential(
    resume_id: UUID,
    platform: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a credential for a platform.

    Regression finding 78: previously this endpoint "archived" by
    prefixing the email with `archived_` and blanking the password,
    leaving the row in the DB. GDPR Art.17 ("right to erasure")
    requires actual deletion unless there's a lawful-basis retention
    justification, and the old behavior surfaced as noise in the UI
    (the "deleted" credential reappeared with a corrupted email).

    Now: actual delete. If an audit-log requirement ever emerges, the
    right place is a separate `credential_audit_log` table — not
    mutilating the live row.
    """
    resume = (await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )).scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    cred = (await db.execute(
        select(PlatformCredential).where(
            PlatformCredential.resume_id == resume.id,
            PlatformCredential.platform == platform,
        )
    )).scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    await db.delete(cred)
    await db.commit()
    return {"status": "deleted"}
