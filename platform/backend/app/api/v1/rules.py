"""Role rule management API endpoints."""

from uuid import UUID
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.rule import RoleRule
from app.models.role_config import RoleClusterConfig
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.schemas.rule import RoleRuleOut, RoleRuleCreate, RoleRuleUpdate
from app.utils.audit import log_action

router = APIRouter(prefix="/rules", tags=["rules"])


async def _valid_cluster_names(db: AsyncSession) -> set[str]:
    """Return the set of cluster names that are currently configured.

    Regression finding 63: POST/PATCH used to hardcode `{"infra", "security"}`,
    which pre-dates the admin-configurable role-cluster UI. The live DB now
    has three active clusters (`infra`, `qa`, `security`) and 509 jobs
    already classified as `role_cluster="qa"`, so the hardcoded list
    rejected valid data. Source of truth is `role_cluster_configs` — the
    same table `/api/v1/role-clusters` reads — so any cluster an admin
    adds becomes a valid rule target without code changes.
    """
    result = await db.execute(
        select(RoleClusterConfig.name).where(RoleClusterConfig.is_active == True)  # noqa: E712
    )
    return {row[0] for row in result}


@router.get("")
async def list_rules(
    cluster: str | None = None,
    is_active: bool | None = None,
    page: int = Query(1, ge=1),
    # F319 (closes F108): canonical ``page_size`` + legacy ``per_page``.
    per_page: int | None = Query(default=None, ge=1, le=200, deprecated=True),
    page_size: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    per_page = per_page if per_page is not None else page_size

    query = select(RoleRule)

    if cluster:
        query = query.where(RoleRule.cluster == cluster)
    if is_active is not None:
        query = query.where(RoleRule.is_active == is_active)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(RoleRule.cluster.asc(), RoleRule.base_role.asc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    rules = result.scalars().all()
    items = [RoleRuleOut.model_validate(r) for r in rules]

    # Regression finding 108: unified pagination keys
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


@router.post("", response_model=RoleRuleOut, status_code=201)
async def create_rule(
    body: RoleRuleCreate,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    valid = await _valid_cluster_names(db)
    if body.cluster not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"Cluster must be one of: {', '.join(sorted(valid)) or '(none configured)'}",
        )

    rule = RoleRule(**body.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)

    await log_action(
        db, user, action="rule.create", resource="role_rule",
        request=request, metadata={"rule_id": str(rule.id), "cluster": body.cluster},
    )

    return RoleRuleOut.model_validate(rule)


@router.patch("/{rule_id}", response_model=RoleRuleOut)
async def update_rule(
    rule_id: UUID,
    body: RoleRuleUpdate,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(RoleRule).where(RoleRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    update_data = body.model_dump(exclude_unset=True)

    if "cluster" in update_data:
        valid = await _valid_cluster_names(db)
        if update_data["cluster"] not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"Cluster must be one of: {', '.join(sorted(valid)) or '(none configured)'}",
            )

    for field, value in update_data.items():
        setattr(rule, field, value)

    await db.commit()
    await db.refresh(rule)

    await log_action(
        db, user, action="rule.update", resource="role_rule",
        request=request, metadata={"rule_id": str(rule_id), "fields": list(update_data.keys())},
    )

    return RoleRuleOut.model_validate(rule)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: UUID,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(RoleRule).where(RoleRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    await db.delete(rule)
    await db.commit()

    await log_action(
        db, user, action="rule.delete", resource="role_rule",
        request=request, metadata={"rule_id": str(rule_id)},
    )
