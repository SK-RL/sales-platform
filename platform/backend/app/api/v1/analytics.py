"""Analytics and dashboard API."""

from datetime import datetime, timezone, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, text, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.review import Review
from app.models.pipeline import PotentialClient
from app.models.company import Company, CompanyATSBoard
from app.models.company_contact import CompanyContact
from app.models.application import Application
from app.models.role_config import RoleClusterConfig
from app.models.user import User
from app.api.deps import get_current_user, require_role

router = APIRouter(prefix="/analytics", tags=["analytics"])


async def _get_relevant_clusters(db: AsyncSession) -> list[str]:
    """Return the configurable set of relevant role clusters.

    Regression finding 177: `warm-leads` previously hardcoded
    `["infra", "security"]` inside the jobs subquery, so an admin adding
    a new cluster (e.g. "data", "qa") via `/role-clusters` would see
    jobs in that cluster silently excluded from warm-leads — producing
    pipeline counts that diverged from the rest of the app (which uses
    the config everywhere). Matches the helper in `jobs.py:_get_relevant_clusters`
    — same DB source of truth (`role_cluster_configs`), same fallback
    when no config has been seeded yet.
    """
    result = await db.execute(
        select(RoleClusterConfig.name).where(
            RoleClusterConfig.is_relevant == True,  # noqa: E712
            RoleClusterConfig.is_active == True,  # noqa: E712
        )
    )
    clusters = result.scalars().all()
    return list(clusters) if clusters else ["infra", "security"]


@router.get("/overview")
async def overview(
    # Regression finding 214: `/analytics/overview` previously declared zero
    # query params, so `?days=7`, `?days=abc`, `?junk=value` all returned
    # byte-identical lifetime totals. The Dashboard "Last 7/30/90 days"
    # switcher (when one ships) would have rendered time-windowed copy on
    # top of all-time numbers, silently misleading users. Fix: honour
    # `days` for the FLOW metrics (status counts, accepted/rejected flow,
    # avg relevance on jobs seen in-window) while leaving CARDINALITY
    # metrics un-windowed (`total_companies`, `pipeline_count` — those
    # count "how many distinct things exist" and don't meaningfully get
    # smaller for a narrower window; re-windowing them would under-count
    # every company that happens not to have posted a job in the window).
    # The response now echoes `days` so a caller that sent `?days=-1` and
    # got silently dropped can detect it via the round-tripped value vs
    # their intended request. Same F179 bounds as `/analytics/trends`.
    days: int = Query(30, ge=1, le=365),
    # Regression finding 221: `role_cluster` is the other common filter
    # axis for a landing-card analytics view ("Infra-only numbers" /
    # "Security-only numbers") but overview used to ignore it outright.
    # Runtime-validated against the live cluster catalog because clusters
    # are admin-configurable (same treatment as `/jobs` F218). `relevant`
    # is allow-listed as the UI pseudo-value meaning "all clusters marked
    # is_relevant=True" (matches F106). Unknown values 400 with the
    # allowed list in the detail.
    role_cluster: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # F221: validate role_cluster BEFORE running any queries so we
    # don't pointlessly query the DB to tell the caller their param
    # was garbage. Same shape as the helper in jobs.py/export.py.
    cluster_filter: list[str] | None = None
    if role_cluster:
        catalog = set(
            (await db.execute(select(RoleClusterConfig.name))).scalars().all()
        ) | {"relevant"}
        if role_cluster not in catalog:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400,
                detail="Invalid role_cluster. Must be one of: "
                + ", ".join(sorted(catalog)),
            )
        if role_cluster == "relevant":
            cluster_filter = await _get_relevant_clusters(db)
        else:
            cluster_filter = [role_cluster]

    # Job counts by status, windowed to jobs first_seen_at >= cutoff.
    # F221: apply the cluster filter too when set — without it, "Infra
    # only" would have shown the lifetime all-cluster totals.
    status_q = select(Job.status, func.count(Job.id)).where(
        Job.first_seen_at >= cutoff
    )
    if cluster_filter is not None:
        status_q = status_q.where(Job.role_cluster.in_(cluster_filter))
    status_q = status_q.group_by(Job.status)

    status_counts: dict[str, int] = {}
    for row in await db.execute(status_q):
        status_counts[row[0]] = row[1]

    total_jobs = sum(status_counts.values())

    # F214: cardinality metrics stay un-windowed. `total_companies` is
    # sourced from the Company table (consistent with the Companies page
    # and Monitoring) — previously this used COUNT(DISTINCT
    # jobs.company_id) which undercounts by excluding companies that
    # don't currently have a job row. Kept as lifetime totals regardless
    # of `days` per the F214 rationale above.
    #
    # F221: same rationale for role_cluster — "companies tracked in
    # total" isn't cluster-scoped; a company hiring both DevOps and
    # Security counts once either way.
    total_companies = (await db.execute(select(func.count(Company.id)))).scalar() or 0
    pipeline_count = (await db.execute(select(func.count(PotentialClient.id)))).scalar() or 0

    accepted_count = status_counts.get("accepted", 0)
    rejected_count = status_counts.get("rejected", 0)
    reviewed_count = accepted_count + rejected_count
    acceptance_rate = (accepted_count / reviewed_count) if reviewed_count > 0 else 0

    # F221: avg_relevance is a flow metric so it honors BOTH filters.
    avg_q = select(func.avg(Job.relevance_score)).where(
        Job.relevance_score > 0,
        Job.first_seen_at >= cutoff,
    )
    if cluster_filter is not None:
        avg_q = avg_q.where(Job.role_cluster.in_(cluster_filter))
    avg_relevance = (await db.execute(avg_q)).scalar() or 0

    return {
        # F214: echo the window so a caller can detect silent-drop by
        # comparing to what they sent. FastAPI's 422 already covers typos
        # (`?days=abc`, `?days=-1`); this covers the "caller forgot to
        # include the param" case where they relied on a default they
        # assumed was different.
        "days": days,
        # F221: echo the filter too — `null` vs. a specific cluster
        # lets the caller confirm their request was received the way
        # they intended.
        "role_cluster": role_cluster,
        "total_jobs": total_jobs,
        "by_status": status_counts,
        "total_companies": total_companies,
        "pipeline_count": pipeline_count,
        "pipeline_active": pipeline_count,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "reviewed_count": reviewed_count,
        "acceptance_rate": float(acceptance_rate),
        "avg_relevance_score": round(float(avg_relevance), 2),
    }


@router.get("/sources")
async def sources(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Job.platform, func.count(Job.id)).group_by(Job.platform).order_by(func.count(Job.id).desc())
    )
    return [{"platform": row[0], "count": row[1]} for row in result]


@router.get("/trends")
async def trends(
    # Regression finding 179: `days: int = 30` had no `ge`/`le` bounds, so
    # `?days=-5` returned 200 with `[]` (cutoff became a future timestamp,
    # filter matched nothing). Rejected outright now — a negative window
    # has no meaningful interpretation, and unbounded large windows let a
    # caller request years of daily rows from a small endpoint.
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT
            DATE(first_seen_at) AS day,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE role_cluster = 'infra') AS infra,
            COUNT(*) FILTER (WHERE role_cluster = 'security') AS security,
            COUNT(*) FILTER (WHERE status = 'accepted') AS accepted,
            COUNT(*) FILTER (WHERE status = 'rejected') AS rejected
        FROM jobs
        WHERE first_seen_at >= NOW() - make_interval(days => :days)
        GROUP BY DATE(first_seen_at)
        ORDER BY day
    """).bindparams(days=days))
    return [
        {
            "day": str(row[0]),
            # Keep "date" alias for frontend charts that use date
            "date": str(row[0]),
            "total": row[1],
            "infra": row[2],
            "security": row[3],
            "accepted": row[4],
            "rejected": row[5],
            # Keep legacy "count" and "new_jobs" aliases for backward compat
            "count": row[1],
            "new_jobs": row[1],
        }
        for row in result
    ]


@router.get("/relevant-jobs-trend")
async def relevant_jobs_trend(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-day breakdown of newly-discovered RELEVANT jobs by
    role-cluster AND geography bucket.

    Powers the Analytics page's "What's flowing in" chart — operator
    wants to see "is the relevant pipeline trending up or sideways for
    infra in global-remote vs USA?" without spinning up SQL.

    Differs from ``/trends``:
      * ``/trends`` hardcodes ``infra`` + ``security`` into the
        SELECT and counts every job (including unclassified ones in
        the totals). Stable, but blind to new clusters added via the
        admin role-config UI (qa, devsecops, etc.) and gives no
        geography signal.
      * This endpoint reads the dynamic relevant-cluster list,
        excludes the ``unclassified`` noise, and emits a nested
        breakdown so the frontend can render either a stacked-area
        by cluster, a stacked-area by geography, OR a heatmap of
        ``cluster × geography`` from one query.

    Response shape (one row per day in ``days`` window, including
    days with zero rows so the chart x-axis stays continuous):

      [
        {
          "day":              "2026-04-25",
          "total_relevant":   23,
          "by_cluster":       {"infra": 12, "security": 8, "qa": 3},
          "by_geography":     {"global_remote": 9, "usa_only": 11, "uae_only": 3},
          "by_cluster_geography": {
            "infra:global_remote": 5, "infra:usa_only": 6, "infra:uae_only": 1,
            "security:global_remote": 3, "security:usa_only": 4, "security:uae_only": 1,
            ...
          }
        },
        ...
      ]

    The cluster + geography ENUMS the response uses are also
    returned at the envelope level so the frontend doesn't have to
    walk every row to build its chart legend.
    """
    relevant = await _get_relevant_clusters(db)
    if not relevant:
        # Defensive: if the role-config table is empty (fresh DB,
        # bad migration), return an empty-but-well-shaped response
        # rather than a SQL syntax error from ``IN ()``.
        return {
            "days": days,
            "clusters": [],
            "geographies": [],
            "rows": [],
        }

    # Bind the cluster list as an array — psycopg2 + asyncpg both
    # handle ``= ANY(:clusters)`` cleanly. Geography filter is the
    # routine's three-bucket allow-list (matches what the operator
    # actually cares about; ``""``/null buckets are noise here).
    geographies = ("global_remote", "usa_only", "uae_only")

    sql = text(
        """
        WITH days AS (
          SELECT generate_series(
            DATE(NOW() - make_interval(days => :days)),
            DATE(NOW()),
            '1 day'::interval
          )::date AS day
        ),
        recent_jobs AS (
          SELECT
            DATE(first_seen_at) AS day,
            role_cluster,
            geography_bucket
          FROM jobs
          WHERE first_seen_at >= NOW() - make_interval(days => :days)
            AND role_cluster = ANY(:clusters)
            AND geography_bucket = ANY(:geos)
        ),
        per_day AS (
          SELECT
            day,
            role_cluster,
            geography_bucket,
            COUNT(*) AS n
          FROM recent_jobs
          GROUP BY day, role_cluster, geography_bucket
        )
        SELECT
          d.day::text AS day,
          COALESCE(p.role_cluster, '')   AS cluster,
          COALESCE(p.geography_bucket, '') AS geography,
          COALESCE(p.n, 0)               AS n
        FROM days d
        LEFT JOIN per_day p ON p.day = d.day
        ORDER BY d.day
        """
    ).bindparams(
        days=days,
        clusters=list(relevant),
        geos=list(geographies),
    )

    raw = (await db.execute(sql)).all()

    # Pivot into per-day buckets. The LEFT JOIN gives us at least
    # one row per day (with cluster='' geography='' n=0) so empty
    # days carry a zeroed entry instead of being dropped.
    by_day: dict[str, dict] = {}
    for row in raw:
        day = row[0]
        cluster = row[1]
        geo = row[2]
        n = int(row[3] or 0)
        bucket = by_day.setdefault(day, {
            "day": day,
            "total_relevant": 0,
            "by_cluster": {c: 0 for c in relevant},
            "by_geography": {g: 0 for g in geographies},
            "by_cluster_geography": {},
        })
        if cluster and geo and n:
            bucket["total_relevant"] += n
            bucket["by_cluster"][cluster] = bucket["by_cluster"].get(cluster, 0) + n
            bucket["by_geography"][geo] = bucket["by_geography"].get(geo, 0) + n
            key = f"{cluster}:{geo}"
            bucket["by_cluster_geography"][key] = (
                bucket["by_cluster_geography"].get(key, 0) + n
            )

    rows = sorted(by_day.values(), key=lambda r: r["day"])

    return {
        "days": days,
        "clusters": list(relevant),
        "geographies": list(geographies),
        "rows": rows,
    }


@router.get("/ai-insights")
async def ai_insights(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Generate AI-powered insights about job and company trends using Claude."""
    from app.config import get_settings
    settings = get_settings()

    # ---- Gather stats ----
    status_counts: dict = {}
    result = await db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))
    for row in result:
        status_counts[row[0]] = row[1]

    total_jobs = sum(status_counts.values())
    accepted = status_counts.get("accepted", 0)
    rejected = status_counts.get("rejected", 0)
    reviewed = accepted + rejected
    acceptance_rate = round(accepted / reviewed * 100, 1) if reviewed else 0

    # Source distribution (top 6 by volume, for display/prompt sizing)
    src_result = await db.execute(
        select(Job.platform, func.count(Job.id))
        .group_by(Job.platform)
        .order_by(func.count(Job.id).desc())
        .limit(6)
    )
    sources = [{"platform": r[0], "count": r[1]} for r in src_result]

    # Total number of distinct ATS sources — used in insight copy.
    # Previously this was `COUNT(DISTINCT Job.platform)`, which hid any
    # configured platform whose boards hadn't produced a job row yet
    # (bamboohr, recruitee, wellfound, etc. at the time of regression
    # finding 28). Match the Platforms page instead: union of the distinct
    # platform names from CompanyATSBoard *and* Job. That way the insight
    # copy always agrees with the Platforms tab.
    board_platforms = (await db.execute(
        select(func.distinct(CompanyATSBoard.platform))
    )).scalars().all()
    job_platforms = (await db.execute(
        select(func.distinct(Job.platform))
    )).scalars().all()
    total_sources = len({p for p in (*board_platforms, *job_platforms) if p})

    # Role cluster split
    cluster_result = await db.execute(
        select(Job.role_cluster, func.count(Job.id)).group_by(Job.role_cluster)
    )
    clusters = {r[0]: r[1] for r in cluster_result}

    # 14-day daily trend (weekly buckets for brevity)
    trend_result = await db.execute(text("""
        SELECT
            DATE_TRUNC('week', first_seen_at) AS week,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE role_cluster = 'infra') AS infra,
            COUNT(*) FILTER (WHERE role_cluster = 'security') AS security
        FROM jobs
        WHERE first_seen_at >= NOW() - INTERVAL '42 days'
        GROUP BY DATE_TRUNC('week', first_seen_at)
        ORDER BY week
    """))
    weekly = [
        {"week": str(r[0])[:10], "total": r[1], "infra": r[2], "security": r[3]}
        for r in trend_result
    ]

    # Companies with active hiring in last 30d
    active_companies = (await db.execute(text("""
        SELECT COUNT(DISTINCT company_id) FROM jobs
        WHERE first_seen_at >= NOW() - INTERVAL '30 days'
    """))).scalar() or 0

    # Contacts coverage
    contact_count = (await db.execute(select(func.count(CompanyContact.id)))).scalar() or 0
    verified_contacts = (await db.execute(
        select(func.count(CompanyContact.id)).where(CompanyContact.email_status == "valid")
    )).scalar() or 0

    stats = {
        "total_jobs": total_jobs,
        "new_jobs_30d": status_counts.get("new", 0),
        "accepted": accepted,
        "rejected": rejected,
        "acceptance_rate_pct": acceptance_rate,
        "infra_jobs": clusters.get("infra", 0),
        "security_jobs": clusters.get("security", 0),
        "top_sources": sources,
        "total_sources": total_sources,
        "active_companies_30d": active_companies,
        "total_contacts": contact_count,
        "verified_contacts": verified_contacts,
        "weekly_trend": weekly,
    }

    # ---- AI call ----
    if not settings.anthropic_api_key.get_secret_value():
        return _fallback_insights(stats)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

        prompt = f"""You are a job-market intelligence analyst reviewing data from a sales & job aggregation platform that tracks cloud/infra/security roles.

Here are the current platform statistics:
{stats}

Generate 5–7 concise, actionable insights about:
1. Hiring velocity and trends (is activity accelerating or slowing?)
2. Which role cluster (infra vs security) is hotter right now
3. Source platform performance (where are the best leads coming from?)
4. Contact/outreach readiness (are we well-positioned to reach decision-makers?)
5. Any patterns, anomalies, or recommendations worth acting on this week

Format: Return ONLY a JSON array of insight strings, like:
["Insight 1 here.", "Insight 2 here.", ...]
Each insight should be 1–2 sentences, specific, and reference actual numbers where useful."""

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text_content = message.content[0].text.strip()

        # Parse JSON array
        import json
        start = text_content.find("[")
        end = text_content.rfind("]") + 1
        insights = json.loads(text_content[start:end]) if start != -1 else []
        if not isinstance(insights, list):
            insights = []
    except Exception:
        return _fallback_insights(stats)

    return {
        "insights": insights,
        "stats": stats,
        "ai_generated": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _fallback_insights(stats: dict) -> dict:
    """Rule-based fallback when Anthropic API is unavailable."""
    insights = []
    total = stats["total_jobs"]
    infra = stats["infra_jobs"]
    security = stats["security_jobs"]
    acceptance = stats["acceptance_rate_pct"]
    contacts = stats["total_contacts"]
    verified = stats["verified_contacts"]

    if total:
        source_count = stats.get("total_sources") or len(stats["top_sources"])
        insights.append(f"Platform has {total:,} jobs indexed across {source_count} ATS sources.")

    if infra and security:
        ratio = round(infra / (infra + security) * 100)
        dominant = "infra/cloud" if infra > security else "security/compliance"
        insights.append(f"{dominant.title()} roles dominate at {ratio if infra > security else 100 - ratio}% of relevant listings ({infra:,} infra vs {security:,} security).")

    if acceptance:
        insights.append(f"Current acceptance rate is {acceptance}% — {'strong signal quality' if acceptance >= 20 else 'consider broadening your filters'}.")

    if stats["top_sources"]:
        top = stats["top_sources"][0]
        insights.append(f"{top['platform'].title()} is the top source with {top['count']:,} jobs — prioritize monitoring this platform.")

    if contacts:
        pct = round(verified / contacts * 100) if contacts else 0
        insights.append(f"{contacts:,} contacts tracked; {pct}% have verified emails ({verified:,}) — {'ready for outreach campaigns' if pct >= 30 else 'enrich more companies to improve coverage'}.")

    if stats["active_companies_30d"]:
        insights.append(f"{stats['active_companies_30d']} companies posted new roles in the last 30 days — prime window for outreach.")

    return {
        "insights": insights,
        "stats": stats,
        "ai_generated": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/funnel")
async def funnel(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    total = (await db.execute(select(func.count(Job.id)))).scalar() or 0
    reviewed = (await db.execute(
        select(func.count(Job.id)).where(Job.status.in_(["accepted", "rejected"]))
    )).scalar() or 0
    accepted = (await db.execute(select(func.count(Job.id)).where(Job.status == "accepted"))).scalar() or 0
    in_pipeline = (await db.execute(select(func.count(PotentialClient.id)))).scalar() or 0

    return {
        "stages": [
            {"name": "New Jobs", "count": total},
            {"name": "Reviewed", "count": reviewed},
            {"name": "Accepted", "count": accepted},
            {"name": "In Pipeline", "count": in_pipeline},
        ]
    }


@router.get("/application-funnel")
async def application_funnel(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Application conversion funnel: prepared -> applied -> interview -> offer."""
    result = await db.execute(
        select(Application.status, func.count(Application.id))
        .where(Application.user_id == user.id)
        .group_by(Application.status)
    )
    counts = {row[0]: row[1] for row in result}

    prepared = counts.get("prepared", 0) + counts.get("applied", 0) + counts.get("interview", 0) + counts.get("offer", 0)
    applied = counts.get("applied", 0) + counts.get("interview", 0) + counts.get("offer", 0)
    interview = counts.get("interview", 0) + counts.get("offer", 0)
    offer = counts.get("offer", 0)

    return {
        "stages": [
            {"stage": "prepared", "count": prepared},
            {"stage": "applied", "count": applied},
            {"stage": "interview", "count": interview},
            {"stage": "offer", "count": offer},
            {"stage": "rejected", "count": counts.get("rejected", 0)},
            {"stage": "withdrawn", "count": counts.get("withdrawn", 0)},
        ],
        "conversion": {
            "prepared_to_applied": round(applied / prepared * 100, 1) if prepared else 0,
            "applied_to_interview": round(interview / applied * 100, 1) if applied else 0,
            "interview_to_offer": round(offer / interview * 100, 1) if interview else 0,
        },
    }


@router.get("/applications-by-platform")
async def applications_by_platform(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Application counts grouped by ATS platform."""
    result = await db.execute(
        select(
            Job.platform,
            Application.status,
            func.count(Application.id),
        )
        .join(Job, Application.job_id == Job.id)
        .where(Application.user_id == user.id)
        .group_by(Job.platform, Application.status)
    )

    platform_data: dict[str, dict] = {}
    for platform, status, count in result:
        if platform not in platform_data:
            platform_data[platform] = {"platform": platform, "total": 0, "applied": 0, "interview": 0, "offer": 0}
        platform_data[platform]["total"] += count
        if status in ("applied", "interview", "offer"):
            platform_data[platform][status] += count

    return {"platforms": sorted(platform_data.values(), key=lambda x: x["total"], reverse=True)}


_CANONICAL_REVIEW_DECISIONS = ("accepted", "rejected", "skipped")


@router.get("/review-insights")
async def review_insights(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Insights from review decisions: rejection reasons, acceptance by platform."""
    # F110/F279 — analytics divergence fix. Pre-fix this endpoint did
    # ``GROUP BY decision`` over EVERY distinct value in the column,
    # so legacy verb-form rows (``"accept"``, ``"reject"``, ``"skip"``
    # written before the F73 normalization shipped) inflated
    # ``total_reviewed``. Meanwhile ``/analytics/overview.reviewed_count``
    # uses ``status IN ("accepted","rejected")`` which excludes them —
    # net effect was the same prod data showing 11 reviews on one
    # panel and 41 on another, breaking trust in the acceptance-rate
    # signal that's the platform's primary review-loop metric.
    #
    # Defensive shape: filter the GROUP BY to canonical past-tense
    # values before aggregating. The admin's ``decision_map`` in
    # ``reviews.py`` writes only canonical forms going forward, but
    # this query still has to coexist with whatever legacy rows are
    # already in the DB. Filtering here means new dashboards always
    # converge on the canonical taxonomy regardless of when the
    # cleanup script runs (or whether it runs at all).
    result = await db.execute(
        select(Review.decision, func.count(Review.id))
        .where(Review.decision.in_(_CANONICAL_REVIEW_DECISIONS))
        .group_by(Review.decision)
    )
    decision_counts = {row[0]: row[1] for row in result}

    # Tags (rejection reasons) -- tags is a PostgreSQL text array
    tag_result = await db.execute(text("""
        SELECT tag, COUNT(*) as cnt
        FROM reviews, unnest(tags) AS tag
        WHERE decision = 'rejected' AND array_length(tags, 1) > 0
        GROUP BY tag
        ORDER BY cnt DESC
    """))
    rejection_reasons = [{"tag": row[0], "count": row[1]} for row in tag_result]

    # Acceptance rate by platform
    platform_result = await db.execute(
        select(
            Job.platform,
            Review.decision,
            func.count(Review.id),
        )
        .join(Job, Review.job_id == Job.id)
        .where(Review.decision.in_(["accepted", "rejected"]))
        .group_by(Job.platform, Review.decision)
    )
    platform_data: dict[str, dict] = {}
    for platform, decision, count in platform_result:
        if platform not in platform_data:
            platform_data[platform] = {"platform": platform, "accepted": 0, "rejected": 0, "rate": 0}
        platform_data[platform][decision] = count

    for p in platform_data.values():
        total = p["accepted"] + p["rejected"]
        p["rate"] = round(p["accepted"] / total * 100, 1) if total else 0

    return {
        "total_reviewed": sum(decision_counts.values()),
        "accepted": decision_counts.get("accepted", 0),
        "rejected": decision_counts.get("rejected", 0),
        "skipped": decision_counts.get("skipped", 0),
        "rejection_reasons": rejection_reasons,
        "acceptance_by_platform": sorted(platform_data.values(), key=lambda x: x["rate"], reverse=True),
    }


@router.get("/funding-signals")
async def funding_signals(
    # F179: same bounded-window treatment as `/trends` — `?days=-1`
    # previously produced a 200 with `{items: [], days: -1}` because
    # the future cutoff never matched. `ge=1, le=365` rejects both
    # negative inputs and unreasonably large windows.
    days: int = Query(180, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Companies that recently received funding — sorted by funded_at desc.
    These are the highest-priority outreach targets (expansion mode).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Contact counts per company
    contact_sub = (
        select(
            CompanyContact.company_id,
            func.count(CompanyContact.id).label("total_contacts"),
            func.sum(case((CompanyContact.is_decision_maker.is_(True), 1), else_=0)).label("decision_makers"),
            func.sum(case((CompanyContact.email_status == "valid", 1), else_=0)).label("verified_contacts"),
        )
        .group_by(CompanyContact.company_id)
        .subquery()
    )

    # Job activity counts
    jobs_sub = (
        select(
            Job.company_id,
            func.count(Job.id).label("open_roles"),
        )
        .where(Job.status.in_(["new", "under_review", "accepted"]))
        .group_by(Job.company_id)
        .subquery()
    )

    result = await db.execute(
        select(
            Company.id,
            Company.name,
            Company.domain,
            Company.industry,
            Company.total_funding,
            Company.funding_stage,
            Company.funded_at,
            Company.funding_news_url,
            Company.employee_count,
            Company.is_target,
            contact_sub.c.total_contacts,
            contact_sub.c.decision_makers,
            contact_sub.c.verified_contacts,
            jobs_sub.c.open_roles,
        )
        .outerjoin(contact_sub, Company.id == contact_sub.c.company_id)
        .outerjoin(jobs_sub, Company.id == jobs_sub.c.company_id)
        .where(Company.funded_at >= cutoff)
        .order_by(Company.funded_at.desc(), Company.total_funding_usd.desc().nulls_last())
        .limit(50)
    )

    now = datetime.now(timezone.utc)
    items = []
    for row in result:
        days_ago = (now - row.funded_at).days if row.funded_at else None
        items.append({
            "company_id": str(row.id),
            "company_name": row.name,
            "domain": row.domain or "",
            "industry": row.industry or "",
            "total_funding": row.total_funding or "",
            "funding_stage": row.funding_stage or "",
            "funded_at": row.funded_at.isoformat() if row.funded_at else None,
            "days_since_funded": days_ago,
            "funding_news_url": row.funding_news_url or "",
            "employee_count": row.employee_count or "",
            "is_target": row.is_target,
            "total_contacts": int(row.total_contacts or 0),
            "decision_makers": int(row.decision_makers or 0),
            "verified_contacts": int(row.verified_contacts or 0),
            "open_roles": int(row.open_roles or 0),
        })

    return {"items": items, "total": len(items), "days": days}


@router.get("/warm-leads")
async def warm_leads(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    Return the top companies with active hiring + verified/valid contacts.
    These are the 'warm leads' — companies with verified contacts and recent job activity.
    """
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)

    # F177: read configurable clusters from the DB instead of hardcoding
    # `["infra", "security"]` — admins adding a new cluster now flow
    # through warm-leads without a code change.
    relevant = await _get_relevant_clusters(db)

    # Subquery: jobs in last 30 days per company
    jobs_sub = (
        select(
            Job.company_id,
            func.count(Job.id).label("new_jobs_30d"),
            func.sum(case((Job.first_seen_at >= cutoff_7d, 1), else_=0)).label("new_jobs_7d"),
            func.max(Job.relevance_score).label("top_score"),
        )
        .where(
            Job.first_seen_at >= cutoff_30d,
            Job.role_cluster.in_(relevant),
        )
        .group_by(Job.company_id)
        .subquery()
    )

    # Subquery: contacts with valid emails per company
    contacts_sub = (
        select(
            CompanyContact.company_id,
            func.count(CompanyContact.id).label("total_contacts"),
            func.sum(case((CompanyContact.email_status == "valid", 1), else_=0)).label("verified_contacts"),
            func.sum(case((CompanyContact.is_decision_maker.is_(True), 1), else_=0)).label("decision_makers"),
        )
        .group_by(CompanyContact.company_id)
        .subquery()
    )

    result = await db.execute(
        select(
            Company.id,
            Company.name,
            Company.domain,
            Company.industry,
            Company.total_funding,
            Company.funding_stage,
            Company.is_target,
            jobs_sub.c.new_jobs_30d,
            jobs_sub.c.new_jobs_7d,
            jobs_sub.c.top_score,
            contacts_sub.c.total_contacts,
            contacts_sub.c.verified_contacts,
            contacts_sub.c.decision_makers,
        )
        .join(jobs_sub, Company.id == jobs_sub.c.company_id)
        .join(contacts_sub, Company.id == contacts_sub.c.company_id)
        .where(contacts_sub.c.total_contacts > 0)
        .order_by(
            contacts_sub.c.decision_makers.desc(),
            contacts_sub.c.verified_contacts.desc(),
            jobs_sub.c.new_jobs_7d.desc(),
        )
        .limit(20)
    )

    items = []
    for row in result:
        items.append({
            "company_id": str(row.id),
            "company_name": row.name,
            "domain": row.domain or "",
            "industry": row.industry or "",
            "total_funding": row.total_funding or "",
            "funding_stage": row.funding_stage or "",
            "is_target": row.is_target,
            "new_jobs_30d": int(row.new_jobs_30d or 0),
            "new_jobs_7d": int(row.new_jobs_7d or 0),
            "top_relevance_score": round(float(row.top_score or 0), 1),
            "total_contacts": int(row.total_contacts or 0),
            "verified_contacts": int(row.verified_contacts or 0),
            "decision_makers": int(row.decision_makers or 0),
        })

    return {"items": items}


@router.get("/scoring-signals")
async def get_scoring_signals(
    # Regression finding 102 (follow-up): `scope` lets admins split the
    # list into the three meaningful buckets: `user` → rows where
    # scoring is attributed to a specific reviewer (per-user isolation
    # from F89); `legacy` → rows written before F89 (user_id IS NULL,
    # the shared-pool signals that should decay to zero over time);
    # `all` → both. Default stays `all` so the existing admin dashboard
    # view keeps working without a frontend change.
    scope: Literal["user", "legacy", "all"] = "all",
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get top scoring signals for admin visibility.

    Regression finding 102: the response previously omitted ``user_id``,
    so there was no way for admins to verify Finding 89's per-user
    isolation was working on prod — every row looked identical whether
    it belonged to a specific reviewer or to the legacy shared pool.
    We now return the column verbatim (NULL = legacy shared-pool row,
    UUID = reviewer-scoped), plus the reviewer's email resolved via a
    single batched query so the frontend can render a "reviewer"
    column without N+1 lookups. The ``scope`` query param lets ops
    filter to either bucket when decay-progress or per-user-audit is
    the question.
    """
    from app.models.scoring_signal import ScoringSignal

    query = select(ScoringSignal).order_by(ScoringSignal.weight.desc()).limit(50)
    if scope == "user":
        query = query.where(ScoringSignal.user_id.is_not(None))
    elif scope == "legacy":
        query = query.where(ScoringSignal.user_id.is_(None))
    # scope == "all" → no extra filter

    result = await db.execute(query)
    signals = result.scalars().all()

    # F102: resolve user_id → email in ONE query so the caller doesn't
    # have to N+1-lookup them. `user_email` is None on legacy rows and
    # on rows whose user_id points to a deactivated/deleted user.
    user_ids = {s.user_id for s in signals if s.user_id is not None}
    email_by_id: dict = {}
    if user_ids:
        email_rows = await db.execute(
            select(User.id, User.email).where(User.id.in_(user_ids))
        )
        email_by_id = {row.id: row.email for row in email_rows}

    return {
        "scope": scope,
        "signals": [
            {
                "user_id": str(s.user_id) if s.user_id else None,
                "user_email": email_by_id.get(s.user_id) if s.user_id else None,
                "signal_type": s.signal_type,
                "signal_key": s.signal_key,
                "weight": round(s.weight, 4),
                "source_count": s.source_count,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in signals
        ],
    }
