"""Company management API endpoints."""

from typing import Literal
from uuid import UUID
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, or_, case, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.company import Company, CompanyATSBoard
from app.models.company_contact import CompanyContact, JobContactRelevance
from app.models.company_office import CompanyOffice
from app.models.job import Job
from app.models.role_config import RoleClusterConfig
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.schemas.company import (
    CompanyOut, CompanyCreate, CompanyUpdate, CompanyDetailOut,
    ATSBoardOut, ATSBoardCreate,
)
from app.schemas.company_contact import (
    CompanyContactOut, CompanyContactCreate, CompanyContactUpdate,
    CompanyOfficeOut, JobRelevantContact, OutreachUpdate,
)
from app.schemas.job import JobOut
from app.utils.sql import escape_like

router = APIRouter(prefix="/companies", tags=["companies"])


async def _get_relevant_clusters(db: AsyncSession) -> list[str]:
    """Return the list of role cluster names flagged as relevant.

    Mirrors the helper in `jobs.py` — duplicated (not imported) to keep
    the two router modules decoupled. Falls back to the hardcoded
    `["infra", "security"]` pair if no cluster config exists yet, which
    matches the behavior documented in CLAUDE.md and the
    `_get_relevant_clusters` helper that `jobs.py` already uses for the
    `role_cluster=relevant` pseudo-value.
    """
    result = await db.execute(
        select(RoleClusterConfig.name).where(
            RoleClusterConfig.is_relevant == True,  # noqa: E712
            RoleClusterConfig.is_active == True,  # noqa: E712
        )
    )
    clusters = result.scalars().all()
    return list(clusters) if clusters else ["infra", "security"]


@router.get("/enrichment-coverage")
async def enrichment_coverage(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only visibility into the enrichment pipeline.

    Returns counts by ``enrichment_status`` plus the top recent error
    messages, so an operator can tell at a glance whether the
    ``enrich_target_companies_batch`` beat task is working and where
    it's failing. Prompted by user feedback that "many companies have
    no data"; before this endpoint, the only way to audit coverage was
    to `SELECT COUNT(*) … GROUP BY enrichment_status` by hand.

    The ``with_active_board_total`` count is the denominator that
    matters for the current enrichment batch task (which requires at
    least one active board before it'll queue a company) — a huge gap
    between that and ``enriched`` is the signal that enrichment is
    behind.

    Read-only. No side effects. Admin-gated because the error messages
    can leak domain names, scraped URLs, and upstream API response
    snippets that a non-admin shouldn't see.
    """
    # Count by status — one round-trip. GROUP BY on a non-indexed
    # column is fine here because `companies` is small (~1k rows).
    rows = (await db.execute(
        select(Company.enrichment_status, func.count(Company.id))
        .group_by(Company.enrichment_status)
    )).all()
    by_status: dict[str, int] = {status or "": count for status, count in rows}

    total = sum(by_status.values())

    # Subquery: companies that the batch task would consider — must
    # have at least one active board. Mirrors the predicate in
    # `enrich_target_companies_batch` so the two stay reconcilable.
    from app.models.company import CompanyATSBoard
    from sqlalchemy import exists
    has_active_board = exists().where(
        CompanyATSBoard.company_id == Company.id,
        CompanyATSBoard.is_active.is_(True),
    )
    with_active_board_total = (await db.execute(
        select(func.count(Company.id)).where(has_active_board)
    )).scalar() or 0

    # Top recent errors so the admin doesn't have to pop open the
    # Celery logs. Grouped by message to collapse repeats, capped at
    # 10 distinct messages. The `func.count(…)` filter shape is a
    # standard "top-N of grouped column" pattern.
    error_rows = (await db.execute(
        select(Company.enrichment_error, func.count(Company.id))
        .where(
            Company.enrichment_status == "failed",
            Company.enrichment_error != "",
        )
        .group_by(Company.enrichment_error)
        .order_by(func.count(Company.id).desc())
        .limit(10)
    )).all()
    top_errors = [{"error": msg, "count": count} for msg, count in error_rows]

    return {
        "total": total,
        "by_status": {
            "pending": by_status.get("pending", 0),
            "enriching": by_status.get("enriching", 0),
            "enriched": by_status.get("enriched", 0),
            "failed": by_status.get("failed", 0),
            "other": sum(
                count for status, count in by_status.items()
                if status not in ("pending", "enriching", "enriched", "failed")
            ),
        },
        "with_active_board_total": with_active_board_total,
        "enriched_pct_of_active": (
            round(by_status.get("enriched", 0) / with_active_board_total * 100, 1)
            if with_active_board_total else 0
        ),
        "top_errors": top_errors,
    }


@router.get("/scores")
async def company_scores(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get company scores for filtering — based on relevant job count, remote coverage, avg relevance."""
    from sqlalchemy import Float as SAFloat
    from sqlalchemy.sql.expression import cast

    # F177: use the configurable cluster list instead of the hardcoded
    # `["infra", "security"]` pair, so `relevant_jobs` on this endpoint
    # stays in sync with what `jobs.py` / `resume.py` / `export.py`
    # treat as relevant.
    relevant = await _get_relevant_clusters(db)

    # Subquery: for each company, count relevant jobs, global remote
    # jobs WITHIN THE RELEVANT CLUSTERS, and avg score.
    #
    # F295 (closes F213): pre-fix the ``remote_jobs`` count was
    # ``CASE WHEN geography_bucket='global_remote' THEN 1 ELSE 0``
    # over ALL the company's jobs — so a company with 9 in-cluster
    # jobs and 46 global-remote-but-OUT-OF-CLUSTER jobs (Supabase
    # at the F213 verification time) had ``remote=46, relevant=9``,
    # which then drove ``remote_ratio = (46/9)*20 = 102.22`` —
    # blowing past the 20-point cap and giving the company a
    # 151.9 ``company_score`` against a documented max of 100.
    # The semantic intent of the metric is "what fraction of this
    # company's *relevant* jobs is global-remote?" so the right fix
    # is to count remote-only-within-relevant. Bonus side-effect:
    # the (relevant_jobs > 0) WHERE clause downstream means
    # remote_jobs <= relevant_jobs by construction, so the ratio
    # is naturally ≤ 1.0.
    scores_q = (
        select(
            Job.company_id,
            func.count(Job.id).label("total_jobs"),
            func.sum(case((Job.role_cluster.in_(relevant), 1), else_=0)).label("relevant_jobs"),
            # F295: AND-the-cluster-filter. Asymmetric denominators
            # were the F213 bug.
            func.sum(case(
                (
                    (Job.geography_bucket == "global_remote") &
                    (Job.role_cluster.in_(relevant)),
                    1,
                ),
                else_=0,
            )).label("remote_jobs"),
            func.avg(case((Job.relevance_score > 0, Job.relevance_score), else_=None)).label("avg_score"),
        )
        .group_by(Job.company_id)
        .subquery()
    )

    result = await db.execute(
        select(
            Company.id, Company.name, Company.is_target,
            scores_q.c.total_jobs,
            scores_q.c.relevant_jobs,
            scores_q.c.remote_jobs,
            scores_q.c.avg_score,
        )
        .outerjoin(scores_q, Company.id == scores_q.c.company_id)
        .where(scores_q.c.relevant_jobs > 0)
        .order_by(scores_q.c.relevant_jobs.desc())
        .limit(100)
    )

    items = []
    for row in result:
        relevant = int(row.relevant_jobs or 0)
        remote = int(row.remote_jobs or 0)
        avg_score = float(row.avg_score or 0)
        is_target = row.is_target

        # Company score: weighted combination
        # 40% relevant job count (capped at 20), 25% avg relevance, 20% remote ratio, 15% target bonus
        # F295 (closes F213): the SQL change above already enforces
        # ``remote <= relevant`` by AND-ing the cluster filter, so
        # the ratio is structurally ≤ 1.0. Wrapping in ``min(...)``
        # here is defense-in-depth against any future SQL refactor
        # that breaks the invariant — the documented 100-point
        # ceiling stays load-bearing for downstream filters
        # ("show me companies scoring ≥90") and shouldn't depend on
        # SQL semantics being preserved across edits.
        job_component = min(relevant / 20, 1.0) * 40
        score_component = (avg_score / 100) * 25
        remote_ratio = min(remote / max(relevant, 1), 1.0) * 20
        target_bonus = 15 if is_target else 0
        company_score = round(job_component + score_component + remote_ratio + target_bonus, 1)

        items.append({
            "company_id": str(row.id),
            "company_name": row.name,
            "is_target": is_target,
            "total_jobs": int(row.total_jobs or 0),
            "relevant_jobs": relevant,
            "remote_jobs": remote,
            "avg_relevance_score": round(avg_score, 1),
            "company_score": company_score,
        })

    items.sort(key=lambda x: x["company_score"], reverse=True)
    return {"items": items}


# Regression finding 100: `sort_by` was typed `str` and checked with an
# if/elif chain — unknown values silently fell through to `Company.name
# ASC`. `sort_dir` didn't exist at all, so even the keys that DID sort
# ignored a requested direction. Literal-typed at the parameter level so
# FastAPI 422s typos at parse time with the allowed list in the detail;
# aggregate branches (`job_count`, `accepted_count`, …) now also accept
# ascending direction (e.g. "companies with FEWEST jobs that are still
# actively hiring" is a legitimate ops query that used to be impossible).
# `contact_count` isn't in the allowlist yet because it's the one
# aggregate we compute in Python post-query — adding it as a subquery is
# a nice-to-have but outside the scope of this fix (the UI doesn't ask
# for it today).
CompanySortBy = Literal[
    "name",
    "funded_at",
    "total_funding",
    "relevant_job_count",
    "job_count",
    "accepted_count",
]
CompanySortDir = Literal["asc", "desc"]


@router.get("")
async def list_companies(
    search: str | None = None,
    is_target: bool | None = None,
    has_contacts: bool | None = None,
    actively_hiring: bool | None = None,
    funding_stage: str | None = None,
    recently_funded: bool | None = None,
    sort_by: CompanySortBy = "name",
    sort_dir: CompanySortDir = "desc",
    page: int = Query(1, ge=1),
    # F319 (closes F108): canonical ``page_size`` + legacy ``per_page``.
    per_page: int | None = Query(default=None, ge=1, le=200, deprecated=True),
    page_size: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    per_page = per_page if per_page is not None else page_size

    # Build subqueries for filters
    contact_sub = (
        select(CompanyContact.company_id)
        .group_by(CompanyContact.company_id)
        .having(func.count(CompanyContact.id) > 0)
        .subquery()
    )

    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
    hiring_sub = (
        select(Job.company_id)
        .where(Job.first_seen_at >= cutoff_30d)
        .group_by(Job.company_id)
        .subquery()
    )

    def apply_filters(q):
        if search and search.strip():
            # Findings 84+85: escape `%`/`_`/`\\` so user-legal characters
            # ("100%", "dev_ops") match literally, and guard against
            # whitespace-only input that previously wildcarded rows with
            # triple-spaces in their name/industry/headquarters.
            needle = f"%{escape_like(search.strip())}%"
            q = q.where(
                or_(
                    Company.name.ilike(needle, escape="\\"),
                    Company.industry.ilike(needle, escape="\\"),
                    Company.headquarters.ilike(needle, escape="\\"),
                )
            )
        if is_target is not None:
            q = q.where(Company.is_target == is_target)
        if funding_stage:
            q = q.where(Company.funding_stage == funding_stage)
        if has_contacts is True:
            q = q.where(Company.id.in_(select(contact_sub.c.company_id)))
        elif has_contacts is False:
            q = q.where(Company.id.notin_(select(contact_sub.c.company_id)))
        if actively_hiring is True:
            q = q.where(Company.id.in_(select(hiring_sub.c.company_id)))
        elif actively_hiring is False:
            q = q.where(Company.id.notin_(select(hiring_sub.c.company_id)))
        if recently_funded is True:
            funded_cutoff = datetime.now(timezone.utc) - timedelta(days=180)
            q = q.where(Company.funded_at >= funded_cutoff)
        return q

    # Regression finding 98: build a per-company relevant-job-count
    # subquery so the main SELECT can both surface the value AND sort
    # by it without a second round-trip. Filters by the live role-
    # cluster config (falling back to the hardcoded infra/security
    # pair) so a new cluster flipped to `is_relevant=True` in the
    # admin UI shows up immediately without a backend deploy.
    relevant_clusters = await _get_relevant_clusters(db)
    relevant_count_subq = (
        select(
            Job.company_id.label("company_id"),
            func.count(Job.id).label("cnt"),
        )
        .where(Job.role_cluster.in_(relevant_clusters))
        .group_by(Job.company_id)
        .subquery()
    )
    # `COALESCE(cnt, 0)` so companies with zero matching jobs return 0
    # instead of NULL after the LEFT JOIN. Wrapped in a column label so
    # both the SELECT and the ORDER BY reference the same expression.
    relevant_count_col = func.coalesce(relevant_count_subq.c.cnt, 0).label("relevant_job_count")

    # Regression finding 100: `sort_by=job_count` and `sort_by=accepted_count`
    # were silently falling through to `Company.name ASC` because the handler
    # only recognized funded_at / total_funding / relevant_job_count. Build a
    # subquery for total-job-count and accepted-job-count so the DB can sort
    # before pagination — post-query Python-side counts only apply to the
    # current page, which would give wrong ordering.
    job_count_subq = (
        select(
            Job.company_id.label("company_id"),
            func.count(Job.id).label("cnt"),
        )
        .group_by(Job.company_id)
        .subquery()
    )
    job_count_col = func.coalesce(job_count_subq.c.cnt, 0).label("job_count")

    accepted_count_subq = (
        select(
            Job.company_id.label("company_id"),
            func.count(Job.id).label("cnt"),
        )
        .where(Job.status == "accepted")
        .group_by(Job.company_id)
        .subquery()
    )
    accepted_count_col = func.coalesce(accepted_count_subq.c.cnt, 0).label("accepted_count")

    query = apply_filters(
        select(Company, relevant_count_col, job_count_col, accepted_count_col)
        .outerjoin(relevant_count_subq, Company.id == relevant_count_subq.c.company_id)
        .outerjoin(job_count_subq, Company.id == job_count_subq.c.company_id)
        .outerjoin(accepted_count_subq, Company.id == accepted_count_subq.c.company_id)
        .options(joinedload(Company.ats_boards))
    )
    count_base = apply_filters(select(Company.id))
    total = (await db.execute(select(func.count()).select_from(count_base.subquery()))).scalar() or 0

    # F100: sort_dir is now a first-class param, so `Sort: Most Jobs`
    # + "ascending" is expressible. For the two nullable columns
    # (`funded_at`, `total_funding`) we still push NULLs to the end on
    # both directions — otherwise `sort_by=funded_at&sort_dir=asc` buries
    # every unfunded company at the top, which is the opposite of useful
    # for "surface companies that got funded most recently (starting
    # from the oldest)". Secondary sort on `Company.name.asc()` is
    # preserved for stable pagination when many rows share the primary
    # sort key (e.g. 200 companies with job_count=0 tie in one bucket).
    if sort_by == "name":
        query = query.order_by(
            Company.name.desc() if sort_dir == "desc" else Company.name.asc()
        )
    else:
        sort_col_map = {
            "funded_at": Company.funded_at,
            "total_funding": Company.total_funding_usd,
            "relevant_job_count": relevant_count_col,
            "job_count": job_count_col,
            "accepted_count": accepted_count_col,
        }
        col = sort_col_map[sort_by]
        nullable = sort_by in ("funded_at", "total_funding")
        if sort_dir == "desc":
            primary = col.desc().nulls_last() if nullable else col.desc()
        else:
            primary = col.asc().nulls_last() if nullable else col.asc()
        query = query.order_by(primary, Company.name.asc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    # Rows come back as `(Company, relevant_count, job_count, accepted_count)`
    # tuples because of the labeled subquery columns in the SELECT.
    # `unique()` dedupes the eagerly-loaded `ats_boards` fanout.
    rows = result.unique().all()
    companies = [r[0] for r in rows]
    relevant_counts: dict = {r[0].id: int(r[1] or 0) for r in rows}
    db_job_counts: dict = {r[0].id: int(r[2] or 0) for r in rows}
    db_accepted_counts: dict = {r[0].id: int(r[3] or 0) for r in rows}

    # Contact counts still need a separate query (not in the main SELECT).
    # job_count and accepted_count are now available from the main query's
    # subquery columns — no second round-trip needed for those.
    company_ids = [c.id for c in companies]
    contact_counts = {}
    if company_ids:
        contact_q = (
            select(CompanyContact.company_id, func.count(CompanyContact.id).label("cnt"))
            .where(CompanyContact.company_id.in_(company_ids))
            .group_by(CompanyContact.company_id)
        )
        for row in await db.execute(contact_q):
            contact_counts[row.company_id] = int(row.cnt or 0)

    items = []
    for c in companies:
        item = CompanyOut.model_validate(c)
        item.job_count = db_job_counts.get(c.id, 0)
        item.relevant_job_count = relevant_counts.get(c.id, 0)
        item.accepted_count = db_accepted_counts.get(c.id, 0)
        item.contact_count = contact_counts.get(c.id, 0)
        items.append(item)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


@router.get("/{company_id}", response_model=CompanyOut)
async def get_company(
    company_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Company).options(joinedload(Company.ats_boards)).where(Company.id == company_id)
    )
    company = result.unique().scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    item = CompanyOut.model_validate(company)
    item.contact_count = (await db.execute(
        select(func.count(CompanyContact.id)).where(CompanyContact.company_id == company_id)
    )).scalar() or 0
    return item


@router.post("", response_model=CompanyOut, status_code=201)
async def create_company(
    body: CompanyCreate,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    # Check slug uniqueness
    existing = await db.execute(select(Company).where(Company.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Company with this slug already exists")

    company = Company(**body.model_dump())
    db.add(company)
    await db.commit()
    await db.refresh(company)

    # Reload with ats_boards relationship
    result = await db.execute(
        select(Company).options(joinedload(Company.ats_boards)).where(Company.id == company.id)
    )
    company = result.unique().scalar_one()
    return CompanyOut.model_validate(company)


@router.patch("/{company_id}", response_model=CompanyOut)
async def update_company(
    company_id: UUID,
    body: CompanyUpdate,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Company).options(joinedload(Company.ats_boards)).where(Company.id == company_id)
    )
    company = result.unique().scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(company, field, value)

    await db.commit()
    await db.refresh(company)

    # Reload with relationship
    result = await db.execute(
        select(Company).options(joinedload(Company.ats_boards)).where(Company.id == company.id)
    )
    company = result.unique().scalar_one()
    return CompanyOut.model_validate(company)


@router.get("/{company_id}/jobs")
async def company_jobs(
    company_id: UUID,
    status: str | None = None,
    page: int = Query(1, ge=1),
    # F319 (closes F108): canonical ``page_size`` + legacy ``per_page``.
    per_page: int | None = Query(default=None, ge=1, le=200, deprecated=True),
    page_size: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    per_page = per_page if per_page is not None else page_size

    # Verify company exists
    company_result = await db.execute(select(Company).where(Company.id == company_id))
    if not company_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Company not found")

    query = select(Job).where(Job.company_id == company_id)
    if status:
        query = query.where(Job.status == status)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(Job.first_seen_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    jobs = result.scalars().all()
    items = [JobOut.model_validate(j) for j in jobs]

    # F299 (closes F212) — emit canonical envelope keys to match
    # every other paginated list endpoint in the API. Pre-fix this
    # endpoint emitted ``per_page`` + ``pages`` while ``/companies``,
    # ``/jobs``, ``/reviews``, ``/applications`` etc. all emit
    # ``page_size`` + ``total_pages``. The drift broke any frontend
    # ``<Pagination>`` component keyed on the canonical names —
    # 5-of-7 items rendered as "page 1 of 1" because the
    # ``total_pages`` field it read was undefined. The query param
    # name ``per_page`` stays for backwards compat with existing
    # callers (changing the input would break clients); only the
    # response keys flip to canonical names.
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


@router.post("/{company_id}/ats-boards", response_model=ATSBoardOut, status_code=201)
async def add_ats_board(
    company_id: UUID,
    body: ATSBoardCreate,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    # Verify company exists
    company_result = await db.execute(select(Company).where(Company.id == company_id))
    if not company_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Company not found")

    board = CompanyATSBoard(company_id=company_id, **body.model_dump())
    db.add(board)
    await db.commit()
    await db.refresh(board)
    return ATSBoardOut.model_validate(board)


@router.delete("/{company_id}/ats-boards/{board_id}", status_code=204)
async def remove_ats_board(
    company_id: UUID,
    board_id: UUID,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CompanyATSBoard).where(
            CompanyATSBoard.id == board_id,
            CompanyATSBoard.company_id == company_id,
        )
    )
    board = result.scalar_one_or_none()
    if not board:
        raise HTTPException(status_code=404, detail="ATS board not found")

    await db.delete(board)
    await db.commit()


# ── Company detail with enrichment ──────────────────────────────────────

@router.get("/{company_id}/detail")
async def get_company_detail(
    company_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get full company detail with contacts, offices, and hiring metrics."""
    result = await db.execute(
        select(Company)
        .options(
            joinedload(Company.ats_boards),
            joinedload(Company.contacts),
            joinedload(Company.offices),
        )
        .where(Company.id == company_id)
    )
    company = result.unique().scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Compute hiring metrics
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    active_statuses = ["new", "under_review", "accepted"]

    jobs_30d = (await db.execute(
        select(func.count(Job.id)).where(
            Job.company_id == company_id,
            Job.first_seen_at >= cutoff,
        )
    )).scalar() or 0

    open_roles = (await db.execute(
        select(func.count(Job.id)).where(
            Job.company_id == company_id,
            Job.status.in_(active_statuses),
        )
    )).scalar() or 0

    job_count = (await db.execute(
        select(func.count(Job.id)).where(Job.company_id == company_id)
    )).scalar() or 0

    accepted_count = (await db.execute(
        select(func.count(Job.id)).where(
            Job.company_id == company_id, Job.status == "accepted"
        )
    )).scalar() or 0

    velocity = "high" if jobs_30d > 5 else "medium" if jobs_30d >= 2 else "low"

    out = CompanyDetailOut.model_validate(company)
    out.job_count = job_count
    out.accepted_count = accepted_count
    out.actively_hiring = jobs_30d > 0
    out.hiring_velocity = velocity
    out.total_open_roles = open_roles
    return out


@router.post("/{company_id}/enrich")
async def trigger_enrichment(
    company_id: UUID,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger company enrichment (admin only).

    Regression finding 161: previously two concurrent POSTs for the same
    company both returned HTTP 200 with different `task_id`s — Celery
    ended up with two workers enriching the same row simultaneously,
    double-spending external API quota (Clearbit etc.) and racing on
    contact/metadata writes. We now claim the slot atomically in SQL
    before queueing: an UPDATE that flips the row to `enriching` only
    if the row isn't already `enriching` (or has been stuck in that
    state for > 5 minutes — recovery from a crashed worker). If zero
    rows are updated, another request beat us and we return 409 without
    queuing a duplicate task.

    The 5-minute staleness window balances "don't queue two workers for
    the same company" with "don't lock a company forever if a worker
    dies before setting a terminal status". Normal enrichment runs
    complete in < 2 minutes.
    """
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Atomic claim: flip status to `enriching` only if nobody else holds
    # a recent claim. `updated_at` is already an `onupdate` column, so
    # setting `enrichment_status` alone bumps it — we set it explicitly
    # for clarity. The WHERE clause uses `OR updated_at < stale_cutoff`
    # so a worker that died mid-run doesn't hold the slot forever.
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    claim = await db.execute(
        update(Company)
        .where(
            Company.id == company_id,
            or_(
                Company.enrichment_status != "enriching",
                Company.updated_at < stale_cutoff,
            ),
        )
        .values(enrichment_status="enriching", updated_at=datetime.now(timezone.utc))
    )
    await db.commit()

    if claim.rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail="Enrichment already in progress for this company — wait for it to finish or try again in a few minutes",
        )

    from app.workers.tasks.enrichment_task import enrich_company
    task = enrich_company.delay(str(company_id))
    return {"task_id": task.id, "status": "queued", "company_id": str(company_id)}


@router.get("/{company_id}/enrichment-status")
async def enrichment_status(
    company_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check enrichment status for a company."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    contact_count = (await db.execute(
        select(func.count(CompanyContact.id)).where(CompanyContact.company_id == company_id)
    )).scalar() or 0

    return {
        "status": company.enrichment_status,
        "enriched_at": company.enriched_at,
        "error": company.enrichment_error,
        "contacts_count": contact_count,
        "has_website": bool(company.website),
        "has_domain": bool(company.domain),
    }


# ── Contacts CRUD ───────────────────────────────────────────────────────

@router.get("/{company_id}/contacts")
async def list_contacts(
    company_id: UUID,
    role_category: str | None = None,
    # F318 (data correctness, DoS bound): pre-fix this returned ALL
    # contacts unbounded — a company with 5k contacts would dump
    # 5k × ~30-field dicts in one response (~1.5MB). Same DoS class
    # as F107 export; bounded here at 200 default / 1000 max which
    # comfortably covers any real company's contact list while
    # gating against the unbounded enterprise-import case. Page +
    # canonical envelope so the frontend doesn't have to special-
    # case this list vs every other paginated list endpoint.
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(CompanyContact).where(CompanyContact.company_id == company_id)
    if role_category:
        query = query.where(CompanyContact.role_category == role_category)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = (
        query.order_by(
            CompanyContact.seniority.asc(),
            CompanyContact.last_name.asc(),
            # F271-style stable tiebreaker so paged traversal
            # doesn't shift rows between pages.
            CompanyContact.id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await db.execute(query)
    contacts = result.scalars().all()
    return {
        "items": [CompanyContactOut.model_validate(c) for c in contacts],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total else 1,
    }


# Regression finding 160: company contacts had no (company_id, email)
# uniqueness guard. In production this allowed the same email to be
# inserted repeatedly at the same company — bulk CSV imports created
# 3-5 copies per contact, outreach status would get reset on each
# duplicate insert, and the UI showed the same person N times with
# different outreach states. We can't safely add a UNIQUE INDEX in this
# PR because the table already contains legacy duplicates that would
# fail the migration; fix at the handler instead so no NEW duplicates
# sneak in, and flag the backfill for a separate migration PR.
# Case-insensitive compare because manual entry / CSV imports routinely
# hit `Jane@acme.com` / `jane@acme.com` variants on the same person.
def _normalise_email(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().lower()


async def _email_already_exists(
    db: AsyncSession,
    company_id: UUID,
    email: str,
    *,
    exclude_contact_id: UUID | None = None,
) -> UUID | None:
    """Return the id of an existing contact with this email at this
    company, or `None` if no duplicate exists. Empty/whitespace-only
    emails are treated as "no email" — duplicates of empty-string are
    allowed because they represent separate people with missing data.
    """
    normalised = _normalise_email(email)
    if not normalised:
        return None
    query = select(CompanyContact.id).where(
        CompanyContact.company_id == company_id,
        func.lower(func.trim(CompanyContact.email)) == normalised,
    )
    if exclude_contact_id is not None:
        query = query.where(CompanyContact.id != exclude_contact_id)
    existing = (await db.execute(query.limit(1))).scalar_one_or_none()
    return existing


@router.post("/{company_id}/contacts", response_model=CompanyContactOut, status_code=201)
async def create_contact(
    company_id: UUID,
    body: CompanyContactCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Company).where(Company.id == company_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Company not found")

    # F160: reject duplicate (company_id, email) pairs so bulk CSV imports
    # can't silently clobber outreach state on re-run. Surfaces the
    # existing row id so the UI can link the user to it instead of a
    # generic "already exists".
    duplicate_id = await _email_already_exists(db, company_id, body.email)
    if duplicate_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A contact with this email already exists at this company",
                "existing_contact_id": str(duplicate_id),
            },
        )

    contact = CompanyContact(
        company_id=company_id,
        source="manual",
        confidence_score=1.0,
        **body.model_dump(),
    )
    db.add(contact)
    # F282 (closes F160) — translate the partial-UNIQUE-violation race
    # into a clean 409. The handler check above is TOCTOU-vulnerable
    # under concurrent POSTs (two callers both pass the SELECT, both
    # insert), so we ALSO catch the IntegrityError that the
    # ``uq_company_contacts_company_email`` partial index raises and
    # surface it as the same 409 the handler check would have produced.
    # Other IntegrityErrors (e.g. FK to a deleted Company) re-raise so
    # the original 500 surfaces with full traceback for ops debugging.
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        constraint = getattr(getattr(exc, "orig", None), "diag", None)
        constraint_name = getattr(constraint, "constraint_name", "") if constraint else ""
        if "uq_company_contacts_company_email" in (constraint_name or "") \
                or "uq_company_contacts_company_email" in str(exc).lower():
            # Re-fetch the winning row's id so the response can point
            # the UI at the existing contact, matching the handler-
            # check 409 shape.
            existing_id = await _email_already_exists(db, company_id, body.email)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "A contact with this email already exists at this company",
                    "existing_contact_id": str(existing_id) if existing_id else None,
                },
            )
        raise
    await db.refresh(contact)
    return CompanyContactOut.model_validate(contact)


@router.patch("/{company_id}/contacts/{contact_id}", response_model=CompanyContactOut)
async def update_contact(
    company_id: UUID,
    contact_id: UUID,
    body: CompanyContactUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CompanyContact).where(
            CompanyContact.id == contact_id,
            CompanyContact.company_id == company_id,
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    update_data = body.model_dump(exclude_unset=True)

    # F160: mirror the duplicate guard on update so you can't rename a
    # contact's email to collide with another. Exclude the current row
    # from the check (otherwise updating `jane@acme.com` to itself
    # would 409). Only check when email actually changes.
    if "email" in update_data:
        new_email = _normalise_email(update_data["email"])
        current_email = _normalise_email(contact.email)
        if new_email and new_email != current_email:
            duplicate_id = await _email_already_exists(
                db, company_id, new_email, exclude_contact_id=contact_id,
            )
            if duplicate_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "A contact with this email already exists at this company",
                        "existing_contact_id": str(duplicate_id),
                    },
                )

    for field, value in update_data.items():
        setattr(contact, field, value)

    await db.commit()
    await db.refresh(contact)
    return CompanyContactOut.model_validate(contact)


@router.delete("/{company_id}/contacts/{contact_id}", status_code=204)
async def delete_contact(
    company_id: UUID,
    contact_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CompanyContact).where(
            CompanyContact.id == contact_id,
            CompanyContact.company_id == company_id,
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    await db.delete(contact)
    await db.commit()


@router.get("/{company_id}/relevant-contacts/{job_id}")
async def relevant_contacts_for_job(
    company_id: UUID,
    job_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get contacts relevant to a specific job at this company."""
    result = await db.execute(
        select(JobContactRelevance, CompanyContact)
        .join(CompanyContact, JobContactRelevance.contact_id == CompanyContact.id)
        .where(
            JobContactRelevance.job_id == job_id,
            CompanyContact.company_id == company_id,
        )
        .order_by(JobContactRelevance.relevance_score.desc())
    )

    items = []
    for rel, contact in result:
        items.append(JobRelevantContact(
            contact=CompanyContactOut.model_validate(contact),
            relevance_reason=rel.relevance_reason,
            relevance_score=rel.relevance_score,
        ))

    return {"items": items}


# ── Outreach workflow ───────────────────────────────────────────────────

_VALID_OUTREACH = {"not_contacted", "emailed", "replied", "meeting_scheduled", "not_interested"}


@router.patch("/{company_id}/contacts/{contact_id}/outreach", response_model=CompanyContactOut)
async def update_contact_outreach(
    company_id: UUID,
    contact_id: UUID,
    body: OutreachUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update outreach status and note for a contact."""
    if body.outreach_status not in _VALID_OUTREACH:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid outreach_status. Must be one of: {', '.join(sorted(_VALID_OUTREACH))}",
        )

    result = await db.execute(
        select(CompanyContact).where(
            CompanyContact.id == contact_id,
            CompanyContact.company_id == company_id,
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    prev_status = contact.outreach_status
    contact.outreach_status = body.outreach_status
    contact.outreach_note = body.outreach_note
    # Stamp last_outreach_at whenever status changes away from not_contacted
    if prev_status != body.outreach_status and body.outreach_status != "not_contacted":
        contact.last_outreach_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(contact)
    return CompanyContactOut.model_validate(contact)


@router.post("/{company_id}/contacts/{contact_id}/draft-email")
async def draft_contact_email(
    company_id: UUID,
    contact_id: UUID,
    # F181: was `str | None` — garbage uuid made it to the SQL cast
    # and returned 500. UUID validates at the FastAPI boundary.
    job_id: UUID | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a personalized outreach email draft for a contact using AI."""
    result = await db.execute(
        select(CompanyContact).where(
            CompanyContact.id == contact_id,
            CompanyContact.company_id == company_id,
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    company_result = await db.execute(select(Company).where(Company.id == company_id))
    company = company_result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    job = None
    if job_id:
        job_result = await db.execute(select(Job).where(Job.id == job_id))
        job = job_result.scalar_one_or_none()

    from app.config import get_settings
    settings = get_settings()

    # Build context
    ctx: list[str] = [
        f"Company: {company.name}",
        f"Contact: {contact.first_name} {contact.last_name}, {contact.title}",
    ]
    if company.industry:
        ctx.append(f"Industry: {company.industry}")
    if company.description:
        ctx.append(f"Company: {company.description[:250]}")
    if company.total_funding:
        ctx.append(f"Funding: {company.total_funding}")
    if company.tech_stack:
        ctx.append(f"Tech stack: {', '.join(company.tech_stack[:8])}")
    if job:
        ctx.append(f"Open role: {job.title}")

    template_subject = f"Quick question for {contact.first_name}"
    template_body = (
        f"Hi {contact.first_name},\n\n"
        f"I came across {company.name} and noticed you're {contact.title}. "
        f"We help companies like yours with cloud infrastructure and security staffing. "
        f"Would you have 15 minutes for a quick call?\n\nBest,"
    )

    if not settings.anthropic_api_key.get_secret_value():
        return {"subject": template_subject, "body": template_body, "generated_by": "template"}

    try:
        import anthropic
        ai = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
        prompt = (
            f"Write a concise, personalized cold outreach email to {contact.first_name} {contact.last_name} "
            f"({contact.title}) at {company.name}. "
            "This is from a cloud infrastructure and security consulting/staffing firm. "
            "Keep it to 3-4 sentences, non-spammy, and reference something specific about the company.\n\n"
            "Context:\n" + "\n".join(ctx) + "\n\n"
            "Respond with exactly:\n"
            "SUBJECT: <subject line>\n"
            "BODY:\n<email body>"
        )
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        subject = template_subject
        body = text
        if "SUBJECT:" in text:
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if line.startswith("SUBJECT:"):
                    subject = line.replace("SUBJECT:", "").strip()
                if line.startswith("BODY:"):
                    body = "\n".join(lines[i + 1:]).strip()
                    break
        return {"subject": subject, "body": body, "generated_by": "claude"}
    except Exception as exc:
        # F329 regression fix: pre-fix, ANY Anthropic-side failure
        # (rate-limit 429, timeout, content-block, transient
        # 5xx) silently rolled back to the deterministic template
        # response with ``generated_by: "template"``. The frontend
        # rendered the template as if the AI had produced it, the
        # user (admin composing recruitment outreach) believed
        # they were sending a personalised AI-customised email,
        # and the recipient got an obvious form letter — credibility
        # and reply-rate damage with no operator signal.
        #
        # Now: still return 200 with the template body so the
        # admin always has SOMETHING to send (UX continuity), but
        # surface ``error: True`` + ``error_message`` so the
        # frontend can render a clear "AI failed, this is the
        # fallback template — edit before sending" notice. The
        # ``generated_by: "template_fallback"`` discriminator is
        # distinct from the no-key ``generated_by: "template"``
        # path above so observability can tell "key not configured"
        # apart from "key configured but call failed".
        #
        # We log the underlying exception type at WARNING level
        # without leaking the API key or upstream stack — admin
        # logs catch the failure class (anthropic.RateLimitError,
        # anthropic.APITimeoutError, etc.) so they can tune limits
        # or retry timing without exposing infra detail to the
        # client.
        import logging
        logging.getLogger(__name__).warning(
            "draft_contact_email AI call failed (%s); falling back "
            "to template",
            exc.__class__.__name__,
        )
        return {
            "subject": template_subject,
            "body": template_body,
            "generated_by": "template_fallback",
            "error": True,
            "error_message": (
                "AI personalisation is temporarily unavailable. The "
                "fallback template below is generic — edit before "
                "sending."
            ),
        }


@router.post("/dedup-contacts")
async def trigger_dedup(
    user: User = Depends(require_role("admin")),
):
    """Trigger contact deduplication task (admin only)."""
    from app.workers.tasks.enrichment_task import deduplicate_contacts
    task = deduplicate_contacts.delay()
    return {"task_id": task.id, "status": "queued"}
