"""F360 — the sweeper that makes apply *automatic*.

Everything before this was assisted: a human picked a job, an endpoint
enqueued it. This is the loop that finds work on its own — the thing
that separates "auto-apply" from "a very good apply button".

Off by default, and deliberately hard to turn on
------------------------------------------------
Unattended submission sends real applications to real employers under
the user's name and cannot be undone. So:

* ``auto_apply_enabled`` defaults False.
* ``auto_apply_daily_cap`` defaults 0 — enabling without setting a cap
  still does nothing. There is no "sensible default" that starts
  applying on someone's behalf because they flipped one switch.
* ``auto_apply_min_score`` defaults to 80, higher than the floor used to
  decide what a human is *shown*. The bar for "send this without me
  looking" should be higher than the bar for "put this in front of me".

Every gate is re-checked downstream
-----------------------------------
This task only *selects* and *enqueues*. It does not decide whether an
application is safe to send — ``submit_application_task`` re-reads the
form and the answers at submit time and runs the full F346/F347 gate.
A job picked here that turns out to need a human lands in the review
queue, which is the correct outcome, not a failure of this sweeper.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession

logger = logging.getLogger(__name__)

# Mirrors routine.EXCLUDED_PLATFORMS — aggregators we never auto-apply
# to because their "apply" is a redirect to somewhere else.
_NEVER_AUTO_APPLY = frozenset({"linkedin"})

# Only consider reasonably fresh postings. An old row is more likely to
# be filled or expired, and a wasted unattended application is worse
# than a wasted human glance.
_MAX_POSTING_AGE_DAYS = 21


def _prefs(user):
    from app.schemas.routine import RoutinePreferences

    raw = getattr(user, "routine_preferences", None) or {}
    if not isinstance(raw, dict):
        return RoutinePreferences()
    try:
        return RoutinePreferences.model_validate(raw)
    except Exception:
        # One user's bad JSONB must not wedge the sweep for everyone.
        logger.warning("auto_apply: unparseable preferences for user %s", user.id)
        return RoutinePreferences()


def _submitted_last_24h(session, user_id) -> int:
    """Applications that already consumed today's budget.

    Counts anything the platform has actually sent or is sending —
    submitted, applied, or currently in flight — plus everything further
    down the funnel that came from a send. Excludes needs_user and
    failed: those never reached an employer, so they must not burn cap
    the user could otherwise spend on a job that will.
    """
    from app.models.application import Application

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    return int(session.execute(
        select(func.count(Application.id)).where(
            Application.user_id == user_id,
            Application.status.in_(
                ("in_flight", "submitted", "applied", "interview", "offer")
            ),
            or_(
                Application.submitted_at >= cutoff,
                Application.applied_at >= cutoff,
                Application.updated_at >= cutoff,
            ),
        )
    ).scalar() or 0)


def title_excluded(title: str, keywords: list[str]) -> bool:
    """F372 — does a standing instruction rule this title out?"""
    t = (title or "").lower()
    return any(k and k.lower() in t for k in (keywords or []))


@celery_app.task
def sweep_auto_apply() -> dict:
    """Find eligible jobs for every opted-in user and queue them."""
    from app.models.application import Application
    from app.models.job import Job
    from app.models.routine_kill_switch import RoutineKillSwitch
    from app.models.user import User
    from app.services.submitters import auto_submittable_platforms
    from app.workers.tasks.apply_task import submit_application_task

    platforms = auto_submittable_platforms() - _NEVER_AUTO_APPLY
    if not platforms:
        return {"users": 0, "queued": 0, "reason": "no auto-submittable platforms"}

    session = SyncSession()
    queued_total = 0
    users_acted = 0
    try:
        users = session.execute(select(User)).scalars().all()
        for user in users:
            prefs = _prefs(user)
            if not prefs.auto_apply_enabled or prefs.auto_apply_daily_cap <= 0:
                continue

            # The user's own stop button wins over everything below.
            killed = session.execute(
                select(RoutineKillSwitch).where(RoutineKillSwitch.user_id == user.id)
            ).scalar_one_or_none()
            if killed and killed.disabled:
                continue

            if not getattr(user, "active_resume_id", None):
                logger.info("auto_apply: user %s has no active resume; skipping", user.id)
                continue

            remaining = prefs.auto_apply_daily_cap - _submitted_last_24h(session, user.id)
            if remaining <= 0:
                continue

            # Jobs this user already has an application for — never
            # apply twice to the same posting.
            seen = set(session.execute(
                select(Application.job_id).where(Application.user_id == user.id)
            ).scalars().all())

            stmt = (
                select(Job)
                .where(
                    Job.platform.in_(platforms),
                    Job.status.notin_(("expired", "archived", "rejected")),
                    Job.relevance_score >= prefs.auto_apply_min_score,
                    Job.posted_at
                    >= datetime.now(timezone.utc) - timedelta(days=_MAX_POSTING_AGE_DAYS),
                )
                .order_by(Job.relevance_score.desc())
                .limit(remaining * 5)  # over-fetch; filtered further below
            )
            if prefs.only_global_remote:
                stmt = stmt.where(Job.geography_bucket == "global_remote")
            elif prefs.allowed_geographies:
                stmt = stmt.where(Job.geography_bucket.in_(list(prefs.allowed_geographies)))
            if prefs.allowed_role_clusters:
                stmt = stmt.where(Job.role_cluster.in_(list(prefs.allowed_role_clusters)))
            if prefs.extra_excluded_platforms:
                stmt = stmt.where(Job.platform.notin_(list(prefs.extra_excluded_platforms)))
            if prefs.excluded_company_ids:
                stmt = stmt.where(
                    Job.company_id.notin_([uuid.UUID(str(c)) for c in prefs.excluded_company_ids])
                )

            picked = 0
            for job in session.execute(stmt).scalars().all():
                if picked >= remaining:
                    break
                if job.id in seen:
                    continue
                if title_excluded(getattr(job, "title", ""), prefs.excluded_title_keywords):
                    continue

                app_row = Application(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    job_id=job.id,
                    resume_id=user.active_resume_id,
                    status="prepared",
                    apply_method="api_submit",
                    submission_source="routine",
                    company_id=job.company_id,
                )
                session.add(app_row)
                try:
                    session.commit()
                except Exception:
                    # Almost certainly the uq_application_user_job unique
                    # index — another path created this application
                    # between our `seen` snapshot and now. Skip it.
                    session.rollback()
                    continue

                submit_application_task.delay(str(app_row.id))
                seen.add(job.id)
                picked += 1

            if picked:
                users_acted += 1
                queued_total += picked
                logger.info(
                    "auto_apply: queued %d application(s) for user %s (cap %d)",
                    picked, user.id, prefs.auto_apply_daily_cap,
                )

        return {"users": users_acted, "queued": queued_total}
    finally:
        session.close()
