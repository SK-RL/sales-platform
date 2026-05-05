"""Career page watch management API endpoints."""

from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.scan import CareerPageWatch
from app.models.user import User
from app.api.deps import get_current_user, require_role


router = APIRouter(prefix="/career-pages", tags=["career-pages"])


# Regression finding 174: `career_page.url` had no format validation, so
# the scraper had been accepting display names (`"Yat Labs"`, `"push AI"`),
# bare slugs (`"zfnd"`, `"reifyhealth"`), and partial domains
# (`"speckle.systems"`, `"tinlake.centrifuge"`) as URLs — 117 heterogeneous
# rows. The scraper couldn't actually fetch any of them (hence `last_hash=""`
# for all 117 after 134 check rounds — change detection was effectively dead).
# We can't safely migrate existing rows without domain research per company,
# but we can stop the bleed: new POST/PATCH must supply a proper http(s) URL.
def _validate_career_page_url(v: str) -> str:
    if v is None:
        return v
    stripped = v.strip()
    if not stripped:
        raise ValueError("url must not be empty")
    if len(stripped) > 2048:
        raise ValueError("url too long (max 2048 chars)")
    low = stripped.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise ValueError(
            "url must be a full URL starting with http:// or https:// — "
            "slugs and display names are not accepted"
        )
    return stripped


class CareerPageOut(BaseModel):
    id: UUID
    company_id: UUID | None
    url: str
    last_hash: str
    last_checked_at: datetime | None
    has_changed: bool
    check_count: int
    change_count: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CareerPageCreate(BaseModel):
    # F151 (SSRF defense-in-depth): `extra="forbid"` so unknown fields
    # 422 at the schema layer. Without this, an attacker could smuggle
    # extra keys into the payload that a future endpoint revision
    # might pick up — the blast radius shrinks if the schema is
    # closed.
    model_config = ConfigDict(extra="forbid")

    company_id: UUID | None = None
    url: str
    is_active: bool = True

    @field_validator("url")
    @classmethod
    def _check_url(cls, v):
        return _validate_career_page_url(v)


class CareerPageUpdate(BaseModel):
    # F151: same `extra="forbid"` rationale as CareerPageCreate.
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    is_active: bool | None = None
    company_id: UUID | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, v):
        # PATCH: omitted `url` stays None (partial update). If explicitly
        # provided, apply the same full-URL requirement as POST.
        if v is None:
            return v
        return _validate_career_page_url(v)


# Regression finding 201: career-page mutations drive the discovery
# scraper — a viewer creating `{"url":"http://attacker.example/",...}`
# could coerce the scraper into hitting attacker-controlled URLs, and
# DELETE on a legitimate watch would silently degrade data quality
# for every downstream consumer. All other ops-owned mutation surfaces
# (rules, role-clusters, monitoring/backup) are gated with
# `require_role("admin")`; career-pages was the last one left on
# plain `get_current_user`. GET stays readable for reviewers/viewers
# so they can see which companies are being watched.
_MUTATE_ROLE_GUARD = require_role("admin")


@router.get("")
async def list_career_pages(
    is_active: bool | None = None,
    page: int = Query(1, ge=1),
    # Regression finding 202: the query param name is kept as
    # `per_page` so existing API clients don't break, but the
    # response envelope now returns the canonical `page_size` /
    # `total_pages` keys used by every other list endpoint (see
    # discovery.py:49-55 per F108). A shared `<Pagination>`
    # component was rendering 0/0 on this list because it only
    # understood the canonical keys.
    # F319 (closes F108): legacy ``per_page`` query param stays
    # alongside the canonical ``page_size``. Either one works;
    # if both are supplied the legacy ``per_page`` wins for back-
    # compat. New callers should send ``page_size`` so the param
    # name matches what every other paginated endpoint uses.
    per_page: int | None = Query(default=None, ge=1, le=200, deprecated=True),
    page_size: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    per_page = per_page if per_page is not None else page_size

    query = select(CareerPageWatch)
    if is_active is not None:
        query = query.where(CareerPageWatch.is_active == is_active)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(CareerPageWatch.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    pages = result.scalars().all()
    items = [CareerPageOut.model_validate(p) for p in pages]

    return {
        "items": items,
        "total": total,
        "page": page,
        # F202: unified keys. Frontends reading `per_page` / `pages`
        # are on a deprecated path and should migrate.
        "page_size": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


@router.post("", response_model=CareerPageOut, status_code=201)
async def create_career_page(
    body: CareerPageCreate,
    user: User = Depends(_MUTATE_ROLE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    # Check URL uniqueness
    existing = await db.execute(select(CareerPageWatch).where(CareerPageWatch.url == body.url))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="This URL is already being watched")

    watch = CareerPageWatch(**body.model_dump())
    db.add(watch)
    await db.commit()
    await db.refresh(watch)
    return CareerPageOut.model_validate(watch)


@router.patch("/{page_id}", response_model=CareerPageOut)
async def update_career_page(
    page_id: UUID,
    body: CareerPageUpdate,
    user: User = Depends(_MUTATE_ROLE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(CareerPageWatch).where(CareerPageWatch.id == page_id))
    watch = result.scalar_one_or_none()
    if not watch:
        raise HTTPException(status_code=404, detail="Career page watch not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(watch, field, value)

    await db.commit()
    await db.refresh(watch)
    return CareerPageOut.model_validate(watch)


@router.delete("/{page_id}", status_code=204)
async def delete_career_page(
    page_id: UUID,
    user: User = Depends(_MUTATE_ROLE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(CareerPageWatch).where(CareerPageWatch.id == page_id))
    watch = result.scalar_one_or_none()
    if not watch:
        raise HTTPException(status_code=404, detail="Career page watch not found")

    await db.delete(watch)
    await db.commit()


@router.post("/{page_id}/check")
async def trigger_check(
    page_id: UUID,
    # F201: triggering an immediate re-check also consumes scrape
    # quota on an external target — gate on admin same as the CRUD
    # surface. A non-admin hammering this endpoint in a loop was a
    # cheap DoS against the scraper.
    user: User = Depends(_MUTATE_ROLE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Trigger an immediate check of this career page.

    This marks the page as needing a check. The actual check is performed
    by the background scanner process.
    """
    result = await db.execute(select(CareerPageWatch).where(CareerPageWatch.id == page_id))
    watch = result.scalar_one_or_none()
    if not watch:
        raise HTTPException(status_code=404, detail="Career page watch not found")

    if not watch.is_active:
        raise HTTPException(status_code=400, detail="Career page watch is inactive")

    # Reset last_checked_at to force the scanner to pick it up next cycle
    watch.last_checked_at = None
    await db.commit()

    return {
        "ok": True,
        "message": "Check queued. The page will be scanned on the next cycle.",
        "page_id": str(page_id),
    }
