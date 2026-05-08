"""Job alert configuration API."""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.alert import AlertConfig
from app.models.user import User
from app.api.deps import get_current_user
from app.utils.ssrf import url_is_safe_for_egress

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["alerts"])


# F331 (closes F120): the documented set of webhook channels the
# alert dispatcher knows how to format messages for. Pre-fix the
# request schema accepted ``channel: str`` so a typo
# (``slcak`` instead of ``slack``) silently persisted and the
# dispatcher's ``if channel == "slack": ...`` cascade fell through
# to the default branch — alert never fired, no error to the admin
# at create time. Locking to a Literal 422s typos at parse time.
# The ``"slack"`` and ``"discord"`` values are pre-allocated even
# though only ``"google_chat"`` is currently implemented in the
# dispatcher; that's the documented set the comment on the
# AlertConfig model column declares (``# google_chat | slack |
# email``) so future additions don't require a schema migration.
AlertChannel = Literal["google_chat", "slack", "discord", "email"]


# Regression finding 140 (CWE-918, severity:red): `webhook_url: str`
# with no validator let an admin POST `{"webhook_url":"http://169.254.169.254/opc/v2/instance/"}`
# and have the server-side worker httpx.post() to Oracle Cloud's IMDS,
# yielding instance-role credentials → S3/EFS/full-cloud takeover. The
# Capital One 2019 pattern. Two layers of defense are wired in:
#   1. Schema-time validator (here) blocks the URL at create/update
#      time so malicious rows can't be persisted in the first place.
#   2. Runtime re-check in `alert_task.send_google_chat_alert` (so
#      pre-existing rows from before this fix can't be exploited via
#      the periodic worker, AND so DNS-rebinding attacks where the
#      A record changes between create-time and request-time still
#      get caught).
def _validate_webhook_url(v: str) -> str:
    if v is None:
        return v
    stripped = v.strip()
    if not stripped:
        raise ValueError("webhook_url must not be empty")
    if len(stripped) > 2048:
        raise ValueError("webhook_url too long (max 2048 chars)")
    allowed, reason = url_is_safe_for_egress(stripped)
    if not allowed:
        # Don't echo `reason` back to the client — it would leak which
        # internal hosts/networks are reachable from the server (e.g.
        # `resolved_ip_blocked:internal.svc->172.17.0.5` confirms the
        # Docker bridge subnet for the attacker). Generic message,
        # detailed reason in server logs only.
        logger.warning("F140: rejected webhook_url=%r reason=%s", stripped[:200], reason)
        raise ValueError(
            "webhook_url is not allowed — must be an https URL on a known webhook provider "
            "(Slack, Google Chat, Discord, Microsoft Teams) or a public host."
        )
    return stripped


class AlertConfigCreate(BaseModel):
    # F140 defense-in-depth: `extra="forbid"` so unknown fields 422 at
    # the schema layer. An attacker can't smuggle e.g. `internal_target`
    # into the payload hoping a future endpoint revision picks it up.
    model_config = ConfigDict(extra="forbid")

    # F331: was ``channel: str`` — a typo silently persisted and
    # the dispatcher fell through to a default branch that never
    # fired. Now Literal-validated so ``slcak`` 422s at parse time.
    channel: AlertChannel = "google_chat"
    webhook_url: str
    min_relevance_score: int = 70
    role_clusters: list[str] | None = None
    geography_filter: str | None = None

    @field_validator("webhook_url")
    @classmethod
    def _check_url(cls, v):
        return _validate_webhook_url(v)


class AlertConfigUpdate(BaseModel):
    # F140: same `extra="forbid"` rationale as AlertConfigCreate.
    model_config = ConfigDict(extra="forbid")

    webhook_url: str | None = None
    min_relevance_score: int | None = None
    role_clusters: list[str] | None = None
    geography_filter: str | None = None
    is_active: bool | None = None

    @field_validator("webhook_url")
    @classmethod
    def _check_url(cls, v):
        # PATCH: omitted webhook_url stays None (partial update). If
        # explicitly provided, run the same SSRF guard as POST.
        if v is None:
            return v
        return _validate_webhook_url(v)


@router.get("")
async def list_alerts(
    # Regression finding 220(A): previous signature declared zero Query(...)
    # params, so `?type=bogus`, `?limit=-1`, `?limit=999999`, `?junk=x`
    # all silently dropped and returned the identical body (FastAPI drops
    # unknown query params by default). Worse, the response envelope was
    # `{"items":[...]}` with no `total`/`page`/`page_size`/`total_pages`,
    # so a user with many alert configs could not page back through them.
    # WHERE user_id = user.id already scopes to the caller's own rows,
    # so the "unbounded dump" vector is narrower here than on F217
    # (scan-logs dumps 236k rows across all users) — but the same
    # envelope-drift and silent-drop class applies.
    #
    # Fix: declare canonical page/page_size with bounds (matches F205 on
    # /applications, F217 on /scan-logs, F108 on /rules). Unknown params
    # still silently drop without forbid_extra middleware, but typed
    # params now 422 on typos instead of silently being ignored.
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Cheap COUNT — each user's alert set is small; no concern about
    # plan-cost here.
    total = (await db.execute(
        select(func.count(AlertConfig.id)).where(AlertConfig.user_id == user.id)
    )).scalar() or 0

    result = await db.execute(
        select(AlertConfig)
        .where(AlertConfig.user_id == user.id)
        .order_by(AlertConfig.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    configs = result.scalars().all()
    return {
        "items": [
            {
                "id": str(c.id),
                "channel": c.channel,
                "webhook_url": c.webhook_url,
                "min_relevance_score": c.min_relevance_score,
                "role_clusters": json.loads(c.role_clusters) if c.role_clusters else None,
                "geography_filter": c.geography_filter,
                "is_active": c.is_active,
                "last_triggered_at": c.last_triggered_at.isoformat() if c.last_triggered_at else None,
                "created_at": c.created_at.isoformat(),
            }
            for c in configs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 0,
    }


@router.post("", status_code=201)
async def create_alert(body: AlertConfigCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    config = AlertConfig(
        id=uuid.uuid4(),
        user_id=user.id,
        channel=body.channel,
        webhook_url=body.webhook_url,
        min_relevance_score=body.min_relevance_score,
        role_clusters=json.dumps(body.role_clusters) if body.role_clusters else None,
        geography_filter=body.geography_filter,
    )
    db.add(config)
    await db.commit()
    return {"id": str(config.id), "status": "created"}


@router.put("/{alert_id}")
async def update_alert(alert_id: UUID, body: AlertConfigUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    config = (await db.execute(
        select(AlertConfig).where(AlertConfig.id == alert_id, AlertConfig.user_id == user.id)
    )).scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Alert config not found")

    if body.webhook_url is not None:
        config.webhook_url = body.webhook_url
    if body.min_relevance_score is not None:
        config.min_relevance_score = body.min_relevance_score
    if body.role_clusters is not None:
        config.role_clusters = json.dumps(body.role_clusters) if body.role_clusters else None
    if body.geography_filter is not None:
        config.geography_filter = body.geography_filter or None
    if body.is_active is not None:
        config.is_active = body.is_active
    config.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return {"id": str(config.id), "status": "updated"}


@router.delete("/{alert_id}")
async def delete_alert(alert_id: UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    config = (await db.execute(
        select(AlertConfig).where(AlertConfig.id == alert_id, AlertConfig.user_id == user.id)
    )).scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Alert config not found")
    await db.delete(config)
    await db.commit()
    return {"status": "deleted"}


# Regression finding 140 item (5): the `/test` endpoint is the cheapest
# SSRF probe primitive in the app — it lets an admin trigger an
# arbitrary outbound HTTP POST AT WILL, repeatedly. With no rate limit,
# an attacker who can flip `webhook_url` (admin compromise, session
# hijack, CSRF on the alert mutation) can mass-scan internal Docker
# network ports / IMDS endpoints at HTTP concurrency. The rate limiter
# here caps that to 5 invocations per 5-minute window per user, which
# is plenty for legitimate "click test, see message in Slack, click
# test again to verify edit" workflows.
from app.utils.rate_limit import LoginRateLimiter as _RateLimiter
_alert_test_limiter = _RateLimiter(max_failures=5, window_seconds=300)


@router.post("/{alert_id}/test")
async def test_alert(
    alert_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a test alert to verify webhook connectivity."""
    # F140 item (5): per-user rate limit. Key on user_id so a stolen
    # admin cookie hammering this endpoint hits the cap quickly.
    rl_key = f"alert_test|{user.id}"
    limited, retry_after = await _alert_test_limiter.is_limited(rl_key)
    if limited:
        raise HTTPException(
            status_code=429,
            detail=f"Too many test-alert requests. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    config = (await db.execute(
        select(AlertConfig).where(AlertConfig.id == alert_id, AlertConfig.user_id == user.id)
    )).scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Alert config not found")

    # F140 defense-in-depth: re-validate the persisted webhook_url on
    # every /test call. Catches (a) pre-fix rows that bypassed the
    # schema validator entirely; (b) DNS-rebinding attacks where the
    # hostname's A record changed between create-time and now. Generic
    # 400 to the user (no detail leak); detailed reason logged.
    allowed, reason = url_is_safe_for_egress(config.webhook_url or "")
    if not allowed:
        logger.warning(
            "F140: refused /alerts/%s/test — webhook_url=%r reason=%s user=%s",
            alert_id, (config.webhook_url or "")[:200], reason, user.id,
        )
        # Burn a rate-limit slot so a probe loop doesn't get free
        # reconnaissance once it's already failing — slows scans.
        await _alert_test_limiter.record_failure(rl_key)
        raise HTTPException(
            status_code=400,
            detail="Configured webhook_url is no longer accepted. Please update it.",
        )

    from app.workers.tasks.alert_task import send_google_chat_alert
    test_jobs = [{
        "id": "test-000",
        "title": "Senior Cloud Engineer (Test Alert)",
        "company_name": "Test Company",
        "relevance_score": 85,
        "role_cluster": "infra",
        "geography_bucket": "global_remote",
        "salary_range": "$150k-200k",
        "platform": "greenhouse",
        "min_score": config.min_relevance_score,
    }]

    success = send_google_chat_alert(config.webhook_url, test_jobs)
    # Always burn a rate-limit slot for /test, success or fail. The
    # quota's purpose is reconnaissance throttling, not failure
    # tracking — even successful tests should be capped.
    await _alert_test_limiter.record_failure(rl_key)
    if not success:
        raise HTTPException(502, "Failed to send test alert. Check your webhook URL.")

    return {"status": "sent", "message": "Test alert sent to your Google Chat group"}
