"""Admin API for managing configurable role clusters (relevant job positions)."""

import re
import uuid
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.role_config import RoleClusterConfig
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.utils.audit import log_action

router = APIRouter(prefix="/role-clusters", tags=["role-clusters"])

# Role cluster names are used as URL path params (`/jobs?role_cluster=<name>`)
# and as JSON keys in downstream reports. Restrict to lowercase alphanum +
# underscore/hyphen so a malicious admin can't slip `?`, `/`, or `..` into
# what callers assume is a safe slug (regression finding 20).
_ROLE_CLUSTER_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _normalize_cluster_name(raw: str) -> str:
    """Lowercase + space→underscore, then enforce the slug allowlist."""
    candidate = raw.lower().strip().replace(" ", "_")
    if not candidate:
        raise HTTPException(
            status_code=400,
            detail="Role cluster name cannot be empty",
        )
    if len(candidate) > 40:
        raise HTTPException(
            status_code=400,
            detail="Role cluster name too long (max 40 characters)",
        )
    if not _ROLE_CLUSTER_NAME_RE.match(candidate):
        raise HTTPException(
            status_code=400,
            detail="Role cluster name must contain only lowercase letters, digits, '_' and '-'",
        )
    return candidate


# Regression finding 200: GET returns BOTH `keywords` (comma-string) and
# `keywords_list` (array) for the same record, but POST/PATCH only
# accepted `keywords` as a string. A client that naturally round-tripped
# GET payload back as PATCH sent `keywords: ["a","b"]` and got a 422
# `Input should be a valid string`. Frontend fix ships a list, so widen
# the input shape: accept either a `list[str]` OR a comma-delimited
# `str`; normalize to the stored comma-string form via a `mode="before"`
# validator that runs before pydantic's str-type check (so `max_length`
# still kicks in on the joined form). `_serialize` keeps emitting both
# wire shapes so existing read-only callers are unaffected. A follow-up
# could migrate the column to JSONB, but that's a data-migration task
# beyond this bug fix.
def _coerce_keywords_list(v):
    if v is None:
        return v
    if isinstance(v, list):
        # Join with ", " (matches the canonical form `_serialize` produces
        # when splitting on comma + strip). Filter out empty/blank entries
        # so `["a", "", "  "]` → `"a"`.
        parts = [str(x).strip() for x in v if str(x).strip()]
        return ", ".join(parts)
    return v


class RoleClusterCreate(BaseModel):
    # F268 — extra="forbid" so admin-side typos like ``is_releavant``
    # 422 instead of being silently dropped. Pre-fix, PATCH/POST
    # against role-clusters silently ignored unknown fields, masking
    # admin keystroke errors as no-op responses.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=40)
    display_name: str = Field(..., min_length=1, max_length=120)
    is_relevant: bool = True
    keywords: str = Field(default="", max_length=4000)
    approved_roles: str = Field(default="", max_length=4000)
    sort_order: int = Field(default=0, ge=0, le=1000)

    _coerce_kw = field_validator("keywords", "approved_roles", mode="before")(
        _coerce_keywords_list
    )


class RoleClusterUpdate(BaseModel):
    # F268 — same forbid policy as RoleClusterCreate above. Manual
    # sweep found PATCH /role-clusters/{id} accepted ``foo_bar`` and
    # returned 200, silently dropping the unknown field.
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    is_relevant: bool | None = None
    is_active: bool | None = None
    keywords: str | None = Field(default=None, max_length=4000)
    approved_roles: str | None = Field(default=None, max_length=4000)
    sort_order: int | None = Field(default=None, ge=0, le=1000)

    _coerce_kw = field_validator("keywords", "approved_roles", mode="before")(
        _coerce_keywords_list
    )


def _serialize(rc: RoleClusterConfig) -> dict:
    return {
        "id": str(rc.id),
        "name": rc.name,
        "display_name": rc.display_name,
        "is_relevant": rc.is_relevant,
        "is_active": rc.is_active,
        "keywords": rc.keywords,
        "approved_roles": rc.approved_roles,
        "sort_order": rc.sort_order,
        "keywords_list": [k.strip() for k in rc.keywords.split(",") if k.strip()] if rc.keywords else [],
        "approved_roles_list": [r.strip() for r in rc.approved_roles.split(",") if r.strip()] if rc.approved_roles else [],
        "created_at": rc.created_at.isoformat() if rc.created_at else None,
        "updated_at": rc.updated_at.isoformat() if rc.updated_at else None,
    }


@router.get("")
async def list_role_clusters(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all role clusters. Shows which ones count as 'relevant'."""
    result = await db.execute(
        select(RoleClusterConfig).order_by(RoleClusterConfig.sort_order, RoleClusterConfig.name)
    )
    clusters = result.scalars().all()
    return {
        "items": [_serialize(c) for c in clusters],
        "relevant_clusters": [c.name for c in clusters if c.is_relevant and c.is_active],
    }


@router.post("")
async def create_role_cluster(
    body: RoleClusterCreate,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new role cluster (admin only)."""
    name = _normalize_cluster_name(body.name)

    existing = await db.execute(
        select(RoleClusterConfig).where(RoleClusterConfig.name == name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Role cluster '{name}' already exists")

    # F285 (closes F142(b)): strip HTML from ``display_name`` so a
    # ``<script>alert(1)</script>`` payload can't be persisted and
    # rendered verbatim in the admin UI. Same helper used by the
    # PATCH path above and the feedback model (F162).
    from app.utils.sanitize import strip_html_tags
    cluster = RoleClusterConfig(
        id=uuid.uuid4(),
        name=name,
        display_name=strip_html_tags(body.display_name),
        is_relevant=body.is_relevant,
        is_active=True,
        keywords=body.keywords,
        approved_roles=body.approved_roles,
        sort_order=body.sort_order,
    )
    db.add(cluster)
    await db.commit()
    await db.refresh(cluster)

    await log_action(
        db, user, action="role_cluster.create", resource="role_cluster",
        request=request, metadata={"cluster_id": str(cluster.id), "name": name},
    )

    return _serialize(cluster)


@router.patch("/{cluster_id}")
async def update_role_cluster(
    # Regression finding 199: was `cluster_id: str`, which let malformed
    # paths like `/role-clusters/not-a-uuid` pass the route handler and
    # reach SQLAlchemy, where `RoleClusterConfig.id == "not-a-uuid"`
    # raised `DataError: invalid input syntax for type uuid: "not-a-uuid"`
    # and bubbled as HTTP 500. Typing the param as `UUID` makes FastAPI
    # 422 the bad input at parse time before any DB work happens — same
    # treatment every other `/{id}` path in the codebase already uses
    # (jobs.py:212, career_pages.py:155, pipeline.py, etc.).
    cluster_id: UUID,
    body: RoleClusterUpdate,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a role cluster (admin only)."""
    result = await db.execute(
        select(RoleClusterConfig).where(RoleClusterConfig.id == cluster_id)
    )
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Role cluster not found")

    # F285 (closes F142(a)): block deactivating built-in clusters
    # via PATCH. The DELETE handler already guards built-ins (line
    # 246), but the PATCH path was a one-API-call kill-switch — an
    # admin could flip ``is_active=False`` or ``is_relevant=False``
    # on the ``infra``/``security`` clusters and silently break the
    # entire role-classification pipeline. ``_get_relevant_clusters``
    # filters on ``is_active=True AND is_relevant=True``, so a
    # flipped flag means every infra/security job downstream gets
    # ``relevance_score=0``. Mirror the DELETE guard so the same
    # protection applies to both mutation paths.
    _BUILTIN_CLUSTERS = ("infra", "security")
    deactivation = (
        body.is_active is False or body.is_relevant is False
    )
    if cluster.name in _BUILTIN_CLUSTERS and deactivation:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot disable built-in cluster '{cluster.name}'. "
                "Built-in clusters are required for the platform's "
                "core role-classification pipeline. To stop showing "
                "them in the UI, hide via the admin filter — don't "
                "disable the cluster itself."
            ),
        )

    if body.display_name is not None:
        # F285 (closes F142(b)): strip HTML from ``display_name``
        # so a payload like ``<script>alert(1)</script>`` can't be
        # persisted and rendered into the admin UI verbatim. Same
        # ``strip_html_tags`` helper used by the feedback model
        # (F162) — strict-strip not sanitize, since display names
        # have no legitimate HTML use.
        from app.utils.sanitize import strip_html_tags
        cluster.display_name = strip_html_tags(body.display_name)
    if body.is_relevant is not None:
        cluster.is_relevant = body.is_relevant
    if body.is_active is not None:
        cluster.is_active = body.is_active
    if body.keywords is not None:
        cluster.keywords = body.keywords
    if body.approved_roles is not None:
        cluster.approved_roles = body.approved_roles
    if body.sort_order is not None:
        cluster.sort_order = body.sort_order

    cluster.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cluster)

    await log_action(
        db, user, action="role_cluster.update", resource="role_cluster",
        request=request, metadata={"cluster_id": str(cluster_id), "fields": list(body.model_dump(exclude_unset=True).keys())},
    )

    return _serialize(cluster)


@router.delete("/{cluster_id}")
async def delete_role_cluster(
    # F199: same UUID typing as the PATCH handler above — non-UUID path
    # must 422 at parse time, not 500 from SQLAlchemy DataError.
    cluster_id: UUID,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a role cluster (admin only). Cannot delete built-in infra/security."""
    result = await db.execute(
        select(RoleClusterConfig).where(RoleClusterConfig.id == cluster_id)
    )
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Role cluster not found")

    if cluster.name in ("infra", "security"):
        raise HTTPException(status_code=400, detail="Cannot delete built-in role clusters")

    cluster_name = cluster.name
    await db.delete(cluster)
    await db.commit()

    await log_action(
        db, user, action="role_cluster.delete", resource="role_cluster",
        request=request, metadata={"cluster_id": str(cluster_id), "name": cluster_name},
    )

    return {"ok": True}
