"""Work-time window control plane.

Two surfaces share the ``/work-window`` router:

  * ``/work-window/me/...``           — user reads own state, submits
                                         + lists own extension requests
  * ``/work-window/admin/...``        — admin/super_admin sets per-user
                                         windows, sets one-off overrides,
                                         and reviews extension requests

The user surface is on the auth-deps allowlist so a locked-out user
can still see the lock-out page and ask for an extension. The admin
surface is naturally allowed because admin role short-circuits the
window check.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.database import get_db
from app.models.user import User
from app.models.work_time import WorkTimeExtensionRequest
from app.schemas.work_window import (
    ExtensionDecision,
    ExtensionRequestCreate,
    ExtensionRequestListResponse,
    ExtensionRequestOut,
    WorkWindowOverride,
    WorkWindowResponse,
    WorkWindowUpdate,
    to_response,
)
from app.utils.work_window import (
    format_minute_ist,
    parse_hhmm_to_minute,
    user_can_access_now,
)

router = APIRouter(prefix="/work-window", tags=["work-window"])


# ─────────────────────────────────────────────────────────────────────
# User-facing — own state + extension requests
# ─────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=WorkWindowResponse)
async def get_my_work_window(
    user: User = Depends(get_current_user),
):
    """Return the caller's window config + a fresh "are you in it now?"
    boolean.

    Powers the dashboard banner ("Window: 09:00–18:00 IST · 47 min
    left") and the lock-out screen. Always 200 — even when the user is
    locked out — because this endpoint is on the deps allowlist so the
    UI can fetch state to render the lock screen.
    """
    now_utc = datetime.now(timezone.utc)
    return to_response(
        enabled=user.work_window_enabled,
        start_min=user.work_window_start_min,
        end_min=user.work_window_end_min,
        override_until=user.work_window_override_until,
        within_window_now=user_can_access_now(user, now_utc),
        server_now_utc=now_utc,
    )


@router.post("/me/extension-requests", response_model=ExtensionRequestOut)
async def create_my_extension_request(
    body: ExtensionRequestCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit an "extend my window by N minutes" request to admins.

    Anti-spam: a user can only have one pending request at a time. If
    a previous request is still pending, this returns 409 with a hint
    so the UI can show "you already asked — wait for admin to act"
    instead of stacking duplicate requests in the admin queue.
    """
    existing = (await db.execute(
        select(WorkTimeExtensionRequest.id).where(
            WorkTimeExtensionRequest.user_id == user.id,
            WorkTimeExtensionRequest.status == "pending",
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You already have a pending extension request.",
        )

    req = WorkTimeExtensionRequest(
        id=uuid.uuid4(),
        user_id=user.id,
        requested_minutes=body.requested_minutes,
        reason=body.reason.strip(),
        status="pending",
    )
    db.add(req)
    # F324 (race-safe): the lookup-then-insert above is TOCTOU-
    # vulnerable to concurrent POSTs from the same user (double-
    # click on a slow connection, two browser tabs). The partial
    # UNIQUE ``uq_work_time_pending_per_user`` (migration
    # ``n0o1p2q3r4s5``) closes the race at the DB layer; here we
    # translate the constraint violation into the same 409 the
    # handler check produces so the user-visible outcome is
    # identical regardless of timing. Other IntegrityErrors
    # (FK to deleted user, etc.) propagate as 500 so genuinely-
    # different bugs aren't hidden.
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        constraint = getattr(getattr(exc, "orig", None), "diag", None)
        constraint_name = (
            getattr(constraint, "constraint_name", "") if constraint else ""
        )
        if (
            "uq_work_time_pending_per_user" in (constraint_name or "")
            or "uq_work_time_pending_per_user" in str(exc).lower()
        ):
            raise HTTPException(
                status_code=409,
                detail="You already have a pending extension request.",
            )
        raise
    await db.refresh(req)
    return _serialize_request(req, user)


@router.get(
    "/me/extension-requests",
    response_model=ExtensionRequestListResponse,
)
async def list_my_extension_requests(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Caller's own request history, newest first.

    Used by the lock-out screen to show "your last request was denied
    with note: …" and by a future Settings page section to surface
    longer history.
    """
    base = select(WorkTimeExtensionRequest).where(
        WorkTimeExtensionRequest.user_id == user.id
    )
    total = (await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()
    rows = (await db.execute(
        base.order_by(WorkTimeExtensionRequest.requested_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all()
    return ExtensionRequestListResponse(
        items=[_serialize_request(r, user) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, (total + page_size - 1) // page_size),
    )


# ─────────────────────────────────────────────────────────────────────
# Admin-facing — per-user windows, overrides, request review
# ─────────────────────────────────────────────────────────────────────


@router.get("/admin/users/{user_id}", response_model=WorkWindowResponse)
async def admin_get_user_window(
    user_id: UUID,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Read a target user's window config."""
    target = await _load_target(db, user_id)
    now_utc = datetime.now(timezone.utc)
    return to_response(
        enabled=target.work_window_enabled,
        start_min=target.work_window_start_min,
        end_min=target.work_window_end_min,
        override_until=target.work_window_override_until,
        within_window_now=user_can_access_now(target, now_utc),
        server_now_utc=now_utc,
    )


@router.patch("/admin/users/{user_id}", response_model=WorkWindowResponse)
async def admin_update_user_window(
    user_id: UUID,
    body: WorkWindowUpdate,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a target user's window config (partial PATCH).

    Pre-fix the only way to lock out a user was to flip
    ``is_active=False`` — a coarse hammer that also broke their JWT.
    With this endpoint admin can shape a per-user shift without
    invalidating sessions.
    """
    target = await _load_target(db, user_id)
    if body.enabled is not None:
        target.work_window_enabled = body.enabled
    if body.start_ist is not None:
        target.work_window_start_min = parse_hhmm_to_minute(body.start_ist)
    if body.end_ist is not None:
        target.work_window_end_min = parse_hhmm_to_minute(body.end_ist)
    db.add(target)
    await db.commit()
    await db.refresh(target)
    now_utc = datetime.now(timezone.utc)
    return to_response(
        enabled=target.work_window_enabled,
        start_min=target.work_window_start_min,
        end_min=target.work_window_end_min,
        override_until=target.work_window_override_until,
        within_window_now=user_can_access_now(target, now_utc),
        server_now_utc=now_utc,
    )


@router.post(
    "/admin/users/{user_id}/override",
    response_model=WorkWindowResponse,
)
async def admin_set_override(
    user_id: UUID,
    body: WorkWindowOverride,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Grant a one-off "extend until X" grace, or clear an existing one.

    Sets the column directly (no audit trail beyond the existing row
    update — admin actions on this surface are infrequent enough that
    a separate audit table isn't worth it yet; the ``decided_by``
    column on extension requests covers the request-driven path).
    """
    target = await _load_target(db, user_id)
    target.work_window_override_until = body.override_until
    db.add(target)
    await db.commit()
    await db.refresh(target)
    now_utc = datetime.now(timezone.utc)
    return to_response(
        enabled=target.work_window_enabled,
        start_min=target.work_window_start_min,
        end_min=target.work_window_end_min,
        override_until=target.work_window_override_until,
        within_window_now=user_can_access_now(target, now_utc),
        server_now_utc=now_utc,
    )


@router.get(
    "/admin/extension-requests",
    response_model=ExtensionRequestListResponse,
)
async def admin_list_requests(
    status: str | None = Query(default="pending"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """List extension requests across all users — admin queue.

    Default ``status=pending`` so the empty-state on the admin panel
    is "no pending requests". Pass ``status=all`` (or any non-matching
    string) to drop the filter and see history.
    """
    base = select(WorkTimeExtensionRequest)
    if status and status not in ("all", ""):
        base = base.where(WorkTimeExtensionRequest.status == status)

    total = (await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()
    rows = (await db.execute(
        base.order_by(WorkTimeExtensionRequest.requested_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all()

    items: list[ExtensionRequestOut] = []
    for row in rows:
        # ``requester`` is lazy="joined" on the model so this is a
        # single round-trip — no N+1.
        items.append(_serialize_request(row, row.requester))

    return ExtensionRequestListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, (total + page_size - 1) // page_size),
    )


@router.post(
    "/admin/extension-requests/{request_id}/decision",
    response_model=ExtensionRequestOut,
)
async def admin_decide_request(
    request_id: UUID,
    body: ExtensionDecision,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Approve (with auto-bumped override) or deny a pending request.

    Approve flow:
      1. Compute ``approved_until`` = max(now_utc, current override) +
         requested_minutes. The "max with current override" matters so
         a second approval extends the lock-up rather than shortening
         it (which would happen if we always anchored to ``now_utc``).
      2. Bump the requester's ``work_window_override_until``.
      3. Freeze ``approved_until`` on the row so a (hypothetical)
         re-approval can't silently re-extend further.

    Deny flow: just stamp status + decision_note. No override change.

    Either way: 409 if the request isn't currently ``pending`` — admins
    can't re-decide a closed request, only file a new one.
    """
    req = (await db.execute(
        select(WorkTimeExtensionRequest).where(
            WorkTimeExtensionRequest.id == request_id
        )
    )).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Request already {req.status}",
        )

    target = await _load_target(db, req.user_id)
    now_utc = datetime.now(timezone.utc)

    if body.decision == "approved":
        anchor = now_utc
        if (
            target.work_window_override_until is not None
            and target.work_window_override_until > now_utc
        ):
            # Stack on top of an active override rather than reset it.
            anchor = target.work_window_override_until
        approved_until = anchor + timedelta(minutes=req.requested_minutes)
        target.work_window_override_until = approved_until
        req.approved_until = approved_until
        db.add(target)

    req.status = body.decision
    req.decided_by_user_id = admin.id
    req.decided_at = now_utc
    req.decision_note = body.note.strip()
    db.add(req)
    await db.commit()
    await db.refresh(req)
    return _serialize_request(req, target)


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


async def _load_target(db: AsyncSession, user_id: UUID) -> User:
    user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _serialize_request(
    req: WorkTimeExtensionRequest, requester: User
) -> ExtensionRequestOut:
    """Project ORM row into the wire shape.

    Centralised so the user-name/email join is only written once. The
    admin list path passes ``req.requester`` (lazy="joined"); the
    self-serve path passes the caller themselves.
    """
    return ExtensionRequestOut(
        id=req.id,
        user_id=req.user_id,
        user_name=requester.name,
        user_email=requester.email,
        requested_minutes=req.requested_minutes,
        reason=req.reason,
        status=req.status,  # type: ignore[arg-type]
        requested_at=req.requested_at,
        decided_by_user_id=req.decided_by_user_id,
        decided_at=req.decided_at,
        decision_note=req.decision_note,
        approved_until=req.approved_until,
    )


# Re-export ``format_minute_ist`` for any caller that wants HH:MM
# rendering without re-importing from utils. Keeps the router's public
# surface self-contained.
__all__ = ["router", "format_minute_ist"]
