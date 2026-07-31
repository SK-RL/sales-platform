"""Twice-weekly AI Intelligence Celery task (F237).

One Celery task that produces both kinds of insights in one beat run:

- ``run_weekly_insights`` → fans out per-user generation for every
  user with an active resume + activity in the last 30 days, then
  generates one platform-wide product-improvement set.

Beat schedule wired in ``celery_app.py::beat_schedule``:

  Mon + Thu at 04:00 UTC  (after rescore_jobs at 03:00 so insights
                           see fresh relevance scores).

Idempotency: each run gets one ``generation_id`` UUID. Per-user rows
share that ID. Re-running the task in the same hour produces a fresh
ID + fresh row set (the ``GET /insights/me`` endpoint returns the
latest generation for the user, so older rows just sit in the
table for trend-tracking).

Failure mode: per-user generation errors are logged and swallowed
(one user's bad signal block shouldn't take down the whole run).
The product-insights pass is wrapped in its own try/except for the
same reason.

Cost ballpark: ~50 active users × $0.05 per_user + $0.10 product run
= ~$2.60 per beat run = ~$5.20/week = ~$22/month at current scale.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, case

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession
from app.workers.tasks._ai_insights import (
    generate_product_insights,
    generate_user_insights,
)
from app.models.user import User
from app.models.review import Review
from app.models.job import Job
from app.models.resume import Resume, ResumeScore, AICustomizationLog
from app.models.application import Application
from app.models.insight import UserInsight, ProductInsight

logger = logging.getLogger(__name__)


# ── Signal collection ────────────────────────────────────────────────────────

def _collect_user_signals(session, user: User) -> dict:
    """Pull the last-30-days behavioral signals for one user.

    Returns a dict suitable for direct JSON serialisation into the
    ``UserInsight.input_signals`` column AND for prompting the LLM.
    All numbers are computed from concrete tables — never invented —
    so every insight the LLM produces can cite a real number.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)

    # Reviews this user wrote in the last 30 days, grouped by decision
    # cross-tabbed with the job's role_cluster.
    rev_rows = session.execute(
        select(
            Job.role_cluster,
            Review.decision,
            func.count(Review.id),
        )
        .join(Job, Job.id == Review.job_id)
        .where(
            Review.reviewer_id == user.id,
            Review.created_at >= cutoff,
        )
        .group_by(Job.role_cluster, Review.decision)
    ).all()

    accept_by_cluster: dict[str, dict[str, int]] = {}
    for cluster, decision, cnt in rev_rows:
        c = (cluster or "unclassified")
        accept_by_cluster.setdefault(c, {"accepted": 0, "rejected": 0, "skipped": 0})
        if decision in accept_by_cluster[c]:
            accept_by_cluster[c][decision] = int(cnt)

    # Top rejection tags (review.tags is ARRAY, so unnest then count).
    # Defensive: tags can be NULL; coalesce to empty array.
    tag_rows = session.execute(
        select(
            func.unnest(func.coalesce(Review.tags, [])).label("tag"),
            func.count(Review.id).label("cnt"),
        )
        .where(
            Review.reviewer_id == user.id,
            Review.decision == "rejected",
            Review.created_at >= cutoff,
        )
        .group_by("tag")
        .order_by(func.count(Review.id).desc())
        .limit(10)
    ).all()
    top_rejection_tags = [{"tag": t, "count": int(c)} for t, c in tag_rows if t]

    # Active resume score distribution (only the active resume is
    # surfaced to the user in the UI, so it's the only one that
    # matters for "how am I matching jobs?").
    score_summary = None
    if user.active_resume_id:
        s = session.execute(
            select(
                func.count(ResumeScore.id),
                func.avg(ResumeScore.overall_score),
                func.max(ResumeScore.overall_score),
                func.percentile_cont(0.9).within_group(ResumeScore.overall_score.desc()),
            ).where(ResumeScore.resume_id == user.active_resume_id)
        ).one()
        score_summary = {
            "scored_jobs": int(s[0] or 0),
            "average": round(float(s[1] or 0), 1),
            "best": round(float(s[2] or 0), 1),
            "p90": round(float(s[3] or 0), 1),
        }

    # AI feature usage in the last 7 days (cover-letter / interview-prep
    # / customize). The new `feature` column lets us split these out.
    ai_rows = session.execute(
        select(
            AICustomizationLog.feature,
            func.count(AICustomizationLog.id),
        ).where(
            AICustomizationLog.user_id == user.id,
            AICustomizationLog.success == True,  # noqa: E712
            AICustomizationLog.created_at >= cutoff_7d,
        ).group_by(AICustomizationLog.feature)
    ).all()
    ai_usage_7d = {feat: int(cnt) for feat, cnt in ai_rows}

    # Applications: how many actually shipped in 7d / 30d?
    apps_7d = session.execute(
        select(func.count(Application.id)).where(
            Application.user_id == user.id,
            Application.applied_at >= cutoff_7d,
        )
    ).scalar() or 0
    apps_30d = session.execute(
        select(func.count(Application.id)).where(
            Application.user_id == user.id,
            Application.applied_at >= cutoff,
        )
    ).scalar() or 0

    return {
        "user_id": str(user.id),
        "user_email": user.email,
        "window_days": 30,
        "accept_by_cluster": accept_by_cluster,
        "top_rejection_tags": top_rejection_tags,
        "active_resume_score": score_summary,
        "ai_usage_last_7d": ai_usage_7d,
        "applications_last_7d": int(apps_7d),
        "applications_last_30d": int(apps_30d),
    }


def _collect_product_signals(session) -> dict:
    """Pull platform-wide signals for product-improvement insights.

    Last-7-days bias because the signal-to-noise ratio is highest on
    recent windows; older patterns are usually already known.
    """
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)

    # Top growing rejection categories (last-7-days vs prior 7d)
    cutoff_14d = datetime.now(timezone.utc) - timedelta(days=14)
    recent_tags = dict(session.execute(
        select(
            func.unnest(func.coalesce(Review.tags, [])).label("tag"),
            func.count(Review.id),
        ).where(
            Review.decision == "rejected",
            Review.created_at >= cutoff_7d,
        ).group_by("tag")
    ).all())
    prior_tags = dict(session.execute(
        select(
            func.unnest(func.coalesce(Review.tags, [])).label("tag"),
            func.count(Review.id),
        ).where(
            Review.decision == "rejected",
            Review.created_at >= cutoff_14d,
            Review.created_at < cutoff_7d,
        ).group_by("tag")
    ).all())
    growing_tags = []
    for tag, recent in recent_tags.items():
        if not tag:
            continue
        prior = int(prior_tags.get(tag, 0))
        delta = int(recent) - prior
        if delta > 0 and int(recent) >= 5:  # noise floor
            growing_tags.append({"tag": tag, "recent_7d": int(recent), "prior_7d": prior, "delta": delta})
    growing_tags.sort(key=lambda x: x["delta"], reverse=True)

    # Companies with high accept rate not in is_target=true (signal:
    # target list is stale).
    from app.models.company import Company
    accept_rows = session.execute(
        select(
            Company.id,
            Company.name,
            Company.is_target,
            func.count(Job.id).label("total"),
            # F357: was ``func.sum(func.cast(Job.status == "accepted",
            # func.Integer))`` — ``func.Integer`` is a bogus SQL
            # function, not a type, so SQLAlchemy raised
            # ``'Comparator' object has no attribute '_isnull'`` and
            # the whole product-insights pass died every run. ``case``
            # is the idiom used elsewhere (companies.py accept counts).
            func.sum(case((Job.status == "accepted", 1), else_=0)).label("accepted"),
        )
        .join(Job, Job.company_id == Company.id)
        .where(Job.first_seen_at >= cutoff_7d)
        .group_by(Company.id, Company.name, Company.is_target)
        .having(func.count(Job.id) >= 3)
    ).all()
    untargeted_high_accept = []
    for cid, name, is_target, total, accepted in accept_rows:
        if is_target:
            continue
        rate = (int(accepted or 0) / int(total)) if total else 0
        if rate >= 0.5:
            untargeted_high_accept.append({
                "company_id": str(cid),
                "company_name": name,
                "accept_rate": round(rate, 2),
                "jobs_seen": int(total),
            })
    untargeted_high_accept.sort(key=lambda x: x["accept_rate"], reverse=True)
    untargeted_high_accept = untargeted_high_accept[:10]

    # Active user count + AI feature usage totals
    active_user_count = session.execute(
        select(func.count(func.distinct(Review.reviewer_id))).where(
            Review.created_at >= cutoff_7d
        )
    ).scalar() or 0

    ai_totals = dict(session.execute(
        select(
            AICustomizationLog.feature,
            func.count(AICustomizationLog.id),
        ).where(
            AICustomizationLog.success == True,  # noqa: E712
            AICustomizationLog.created_at >= cutoff_7d,
        ).group_by(AICustomizationLog.feature)
    ).all())
    ai_totals_7d = {feat: int(cnt) for feat, cnt in ai_totals.items()}

    return {
        "window_days": 7,
        "active_users_7d": int(active_user_count),
        "growing_rejection_tags": growing_tags[:10],
        "high_accept_companies_not_targeted": untargeted_high_accept,
        "ai_feature_calls_7d": ai_totals_7d,
    }


def _recent_actioned_product_insights(session, limit: int = 20) -> list[dict]:
    """Pull the admin's recent triage decisions to feed into the
    next product-insights run. Lets the LLM see "we shipped X last
    week, did metric Y move?" and avoid re-suggesting dismissed items.
    """
    rows = session.execute(
        select(ProductInsight)
        .where(ProductInsight.actioned_at.is_not(None))
        .order_by(ProductInsight.actioned_at.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "title": r.title,
            "category": r.category,
            "actioned_status": r.actioned_status,
            "actioned_note": r.actioned_note,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            "actioned_at": r.actioned_at.isoformat() if r.actioned_at else None,
        }
        for r in rows
    ]


# ── Celery task ──────────────────────────────────────────────────────────────

@celery_app.task(name="app.workers.tasks.ai_insights_task.run_weekly_insights")
def run_weekly_insights():
    """Top-level beat task. Generates per-user + product insights.

    Runs Mon + Thu at 04:00 UTC per ``celery_app.py``. Returns a
    summary dict with counts so the Celery monitoring tool can show
    "X users processed, Y product insights produced".
    """
    logger.info("Starting run_weekly_insights")
    session = SyncSession()
    generation_id = uuid.uuid4()

    try:
        cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)

        # Eligible users: have an active resume AND submitted at least
        # one review in the last 30 days. No point generating insights
        # for dormant accounts — the prompt would have nothing to
        # ground itself in.
        eligible_user_ids = session.execute(
            select(func.distinct(Review.reviewer_id))
            .where(Review.created_at >= cutoff_30d)
        ).scalars().all()

        users_processed = 0
        users_failed = 0
        for uid in eligible_user_ids:
            try:
                user = session.execute(
                    select(User).where(User.id == uid)
                ).scalar_one_or_none()
                if not user or not user.active_resume_id:
                    continue
                signals = _collect_user_signals(session, user)
                result = generate_user_insights(signals)
                if result.get("error"):
                    logger.warning(
                        "user insights failed for %s: %s",
                        user.email, result.get("error_message"),
                    )
                    users_failed += 1
                    continue
                row = UserInsight(
                    user_id=user.id,
                    generation_id=generation_id,
                    insights=result["insights"],
                    input_signals=signals,
                    model_version=result["model_version"],
                    prompt_version=result["prompt_version"],
                    input_tokens=result["input_tokens"],
                    output_tokens=result["output_tokens"],
                )
                session.add(row)
                session.commit()
                users_processed += 1
            except Exception as e:
                logger.exception("per-user insights failed for user %s: %s", uid, e)
                users_failed += 1
                session.rollback()

        # Product insights — single platform-wide call.
        product_count = 0
        product_failed = False
        try:
            signals = _collect_product_signals(session)
            prior = _recent_actioned_product_insights(session)
            result = generate_product_insights(signals, prior_actioned=prior)
            if result.get("error"):
                logger.warning(
                    "product insights failed: %s", result.get("error_message")
                )
                product_failed = True
            else:
                for item in result["insights"]:
                    if not isinstance(item, dict):
                        continue
                    row = ProductInsight(
                        generation_id=generation_id,
                        title=str(item.get("title", "Untitled"))[:200],
                        body=str(item.get("body", "")),
                        category=str(item.get("category", "other"))[:50],
                        severity=str(item.get("severity", "medium"))[:20],
                        input_signals=signals,
                        model_version=result["model_version"],
                        prompt_version=result["prompt_version"],
                        input_tokens=result["input_tokens"],
                        output_tokens=result["output_tokens"],
                    )
                    session.add(row)
                    product_count += 1
                session.commit()
        except Exception as e:
            logger.exception("product insights pass failed: %s", e)
            product_failed = True
            session.rollback()

        logger.info(
            "run_weekly_insights complete: gen=%s users_processed=%d users_failed=%d product=%d product_failed=%s",
            generation_id, users_processed, users_failed, product_count, product_failed,
        )
        return {
            "generation_id": str(generation_id),
            "users_processed": users_processed,
            "users_failed": users_failed,
            "product_insights": product_count,
            "product_failed": product_failed,
        }

    except Exception as e:
        logger.exception("run_weekly_insights catastrophic failure: %s", e)
        session.rollback()
        raise
    finally:
        session.close()
