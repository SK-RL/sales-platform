"""In-app login notices — the current user's banner feed.

``GET /notices/me``            → undismissed notices, newest first
``POST /notices/{id}/dismiss`` → dismiss one (own only)

Read-only for the user beyond dismiss; notices are created server-side
(seed / admin task). Scoped strictly to ``user.id`` so one user can
never read or dismiss another's notice.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.models.user_notice import UserNotice
from app.schemas.user_notice import UserNoticeOut

router = APIRouter(prefix="/notices", tags=["notices"])


@router.get("/me", response_model=list[UserNoticeOut])
async def list_my_notices(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Undismissed notices for the caller, newest first. Cheap enough to
    poll on every page load (indexed on user_id, filtered to the small
    undismissed set)."""
    rows = (await db.execute(
        select(UserNotice)
        .where(
            UserNotice.user_id == user.id,
            UserNotice.dismissed_at.is_(None),
        )
        .order_by(UserNotice.created_at.desc())
    )).scalars().all()
    return rows


@router.post("/{notice_id}/dismiss")
async def dismiss_notice(
    notice_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a notice so it stops appearing. Idempotent — dismissing
    an already-dismissed notice is a no-op 200. Scoped to the caller's
    own notices (404 otherwise — never reveal another user's notice)."""
    notice = (await db.execute(
        select(UserNotice).where(
            UserNotice.id == notice_id,
            UserNotice.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not notice:
        raise HTTPException(status_code=404, detail="Notice not found")
    if notice.dismissed_at is None:
        notice.dismissed_at = datetime.now(timezone.utc)
        await db.commit()
    return {"ok": True}
