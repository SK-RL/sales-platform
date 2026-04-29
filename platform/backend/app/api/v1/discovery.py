"""Company discovery API endpoints."""

import logging
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from pydantic import BaseModel, Field

from app.database import get_db
from app.models.discovery import DiscoveryRun, DiscoveredCompany
from app.models.company import Company, CompanyATSBoard
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.schemas.discovery import DiscoveryRunOut, DiscoveredCompanyOut, DiscoveredCompanyUpdate

logger = logging.getLogger(__name__)


# F286 (closes F137 a+b) — bounded + UUID-typed bulk request.
#
# Pre-fix the schema accepted ``list[str]`` with no length cap, so:
#   (a) Malformed UUIDs flowed straight into the ORM and bubbled
#       ``psycopg.DataError`` as an opaque HTTP 500. F126 pattern
#       recurrence — the fix is the same as everywhere else: type
#       the field as ``UUID`` so Pydantic 422s the bad input at parse
#       time before any DB work.
#   (b) Unbounded list → DoS surface: 1000 IDs ran 1000 individual
#       SELECT round-trips because the handler looped per id, not a
#       single ``WHERE id IN (...)``. The schema cap below pairs with
#       a batched IN-clause query in the handlers — keeps a single
#       admin click bounded at ``BULK_IDS_MAX = 200`` so the worst
#       case is ~one DB round-trip with a 200-element ANY-array.
BULK_IDS_MAX = 200


class BulkIdsRequest(BaseModel):
    # ``UUID`` so Pydantic 422s non-UUID strings at parse time.
    # ``max_length`` caps the DoS surface — 200 IDs is well above any
    # legitimate "select all visible on screen" admin click (the
    # UI's discovery list page is 50/page).
    ids: list[UUID] = Field(..., min_length=1, max_length=BULK_IDS_MAX)

router = APIRouter(prefix="/discovery", tags=["discovery"])


@router.get("/runs")
async def list_runs(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(DiscoveryRun)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(DiscoveryRun.started_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    runs = result.scalars().all()
    items = [DiscoveryRunOut.model_validate(r) for r in runs]

    # Regression finding 108: unified pagination keys
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


@router.post("/fingerprint-companies", status_code=202)
async def trigger_fingerprint_existing_companies(
    limit: int = 50,
    only_unfingerprinted: bool = True,
    user: User = Depends(require_role("admin")),
):
    """Dispatch the reverse-discovery fingerprint task.

    Iterates existing ``Company.website`` URLs, runs the ATS fingerprint
    service against each, and seeds ``DiscoveredCompany`` rows for any
    ``(platform, slug)`` pair we don't already have. Returns a Celery
    ``task_id`` the admin UI can poll via the existing scan-status
    endpoint.

    Why this is a separate endpoint from ``POST /discovery/runs``:
    the classic discovery task probes a hardcoded ATS slug list (fast,
    stale-prone). This one takes the flip side — it probes company
    websites from our own DB to detect which ATS each company uses.
    Different mechanic, different cadence (this should run monthly-ish,
    not every scan cycle), so it gets its own trigger.

    ``limit`` caps per-invocation to keep wall-time bounded — default
    50 = ~12-15 min at ~15s per fingerprint fetch. For a full-corpus
    sweep, dispatch multiple times or bump the limit; the task already
    dedups by ``(platform, slug)`` so overlap is safe.
    """
    # Bound the limit so a typo like `?limit=999999` doesn't queue a
    # 10-hour worker burn.
    if limit < 1 or limit > 500:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail="limit must be between 1 and 500 inclusive",
        )

    # Dispatch to Celery. Same failure-mode pattern as
    # `trigger_discovery_run`: if the broker is down we still want
    # the admin UI to show a meaningful error rather than hanging.
    try:
        from app.workers.tasks.discovery_task import fingerprint_existing_companies
        async_result = fingerprint_existing_companies.delay(
            limit=limit,
            only_unfingerprinted=only_unfingerprinted,
        )
        return {
            "task_id": str(async_result.id),
            "status": "queued",
            "limit": limit,
            "only_unfingerprinted": only_unfingerprinted,
        }
    except Exception as exc:
        logger.exception(
            "Failed to dispatch fingerprint_existing_companies: %s", exc
        )
        from fastapi import HTTPException
        raise HTTPException(
            status_code=502,
            detail=f"Celery dispatch failed: {exc}",
        )


@router.post("/runs", response_model=DiscoveryRunOut, status_code=201)
async def trigger_discovery_run(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new discovery run record and dispatch it to the Celery worker.

    Regression finding 186: previously this endpoint inserted a row with
    ``status='pending'`` and returned, expecting a worker to pick it up —
    but no worker polls for pending rows. The run_discovery Celery task
    only created its OWN rows when fired by Celery Beat, so manually
    triggered runs sat pending forever. The fix is direct dispatch:
    insert the pending row, then hand the run_id to the Celery task so
    it flips it to 'running' and executes. If Celery/Redis is down we
    still return the pending row so the UI isn't blocked — a separate
    cleanup task (fix_stuck_discovery_runs) sweeps rows that never got
    picked up.
    """
    run = DiscoveryRun(
        source="manual",
        status="pending",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # F186: dispatch the Celery task that will flip this row from
    # 'pending' → 'running' and execute discovery. Imported here (not
    # at module load) to avoid importing Celery + all its task deps
    # every time the API module is imported, and to keep the API
    # testable without a live Redis.
    try:
        from app.workers.tasks.discovery_task import run_discovery
        run_discovery.delay(run_id=str(run.id))
    except Exception:
        # Celery broker unreachable or task registration failed —
        # log and fall through. The row stays 'pending' and will be
        # reaped by the stuck-pending cleanup task. Deliberately not
        # raising 502 here: the user's intent (record that a run was
        # requested) succeeded; only the async execution hand-off
        # failed, and the UI can retry.
        logger.exception(
            "Failed to dispatch run_discovery task for run_id=%s — "
            "row left in 'pending' state for cleanup sweep",
            run.id,
        )

    return DiscoveryRunOut.model_validate(run)


@router.get("/companies")
async def list_discovered_companies(
    status: str | None = None,
    run_id: UUID | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(DiscoveredCompany)

    if status:
        query = query.where(DiscoveredCompany.status == status)
    if run_id:
        query = query.where(DiscoveredCompany.discovery_run_id == run_id)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(DiscoveredCompany.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    companies = result.scalars().all()
    items = [DiscoveredCompanyOut.model_validate(c) for c in companies]

    # Regression finding 108: unified pagination keys
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


@router.patch("/companies/{company_id}", response_model=DiscoveredCompanyOut)
async def update_discovered_company(
    company_id: UUID,
    body: DiscoveredCompanyUpdate,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    if body.status not in ("added", "ignored"):
        raise HTTPException(status_code=400, detail="Status must be 'added' or 'ignored'")

    result = await db.execute(select(DiscoveredCompany).where(DiscoveredCompany.id == company_id))
    dc = result.scalar_one_or_none()
    if not dc:
        raise HTTPException(status_code=404, detail="Discovered company not found")

    dc.status = body.status
    await db.commit()
    await db.refresh(dc)
    return DiscoveredCompanyOut.model_validate(dc)


@router.post("/companies/{company_id}/import", status_code=201)
async def import_discovered_company(
    company_id: UUID,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Import a discovered company into the main companies table."""
    result = await db.execute(select(DiscoveredCompany).where(DiscoveredCompany.id == company_id))
    dc = result.scalar_one_or_none()
    if not dc:
        raise HTTPException(status_code=404, detail="Discovered company not found")

    if dc.status == "added":
        raise HTTPException(status_code=409, detail="Company has already been imported")

    # Check if a company with the same slug already exists
    slug = dc.slug or dc.name.lower().replace(" ", "-")
    existing = await db.execute(select(Company).where(Company.slug == slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A company with this slug already exists")

    company = Company(
        name=dc.name,
        slug=slug,
        website=dc.careers_url,
        is_target=False,
    )
    db.add(company)

    # If the discovered company came from an ATS platform, create the ATS board link
    if dc.platform and dc.slug:
        from app.models.company import CompanyATSBoard
        board = CompanyATSBoard(
            company_id=company.id,
            platform=dc.platform,
            slug=dc.slug,
            is_active=True,
        )
        db.add(board)

    dc.status = "added"
    await db.commit()
    await db.refresh(company)

    return {
        "ok": True,
        "company_id": str(company.id),
        "name": company.name,
        "slug": company.slug,
    }


@router.post("/companies/bulk-import")
async def bulk_import_discovered(
    body: BulkIdsRequest,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Bulk import discovered companies into the main companies table.

    F286 (closes F137 b): replaced the per-id loop with a single
    ``WHERE id IN (...)`` query. Pre-fix N IDs = N DB round-trips;
    now N IDs = 2 round-trips (one to fetch all candidates, one to
    pre-check existing slugs). Bounded at ``BULK_IDS_MAX = 200`` by
    the schema so the IN-array stays small.
    """
    # 1. Single batched fetch of all candidates by id.
    candidates = (
        await db.execute(
            select(DiscoveredCompany).where(DiscoveredCompany.id.in_(body.ids))
        )
    ).scalars().all()

    # 2. Build the slug set for the candidates so we can pre-check
    #    existing Company rows in ONE query rather than per-iteration.
    candidate_by_id = {dc.id: dc for dc in candidates}
    desired_slugs = {
        (dc.slug or dc.name.lower().replace(" ", "-")): dc.id
        for dc in candidates
        if dc.status != "added"
    }
    existing_slugs: set[str] = set()
    if desired_slugs:
        existing_slugs = set(
            (await db.execute(
                select(Company.slug).where(Company.slug.in_(desired_slugs.keys()))
            )).scalars().all()
        )

    imported = 0
    skipped = 0
    # 3. Walk the input order so the response counts match the
    #    caller's expectation. Missing-by-id (unknown UUIDs) and
    #    already-imported (status="added") both bucket as skipped.
    for dc_id in body.ids:
        dc = candidate_by_id.get(dc_id)
        if not dc or dc.status == "added":
            skipped += 1
            continue
        slug = dc.slug or dc.name.lower().replace(" ", "-")
        if slug in existing_slugs:
            dc.status = "added"
            skipped += 1
            continue
        company = Company(name=dc.name, slug=slug, website=dc.careers_url, is_target=False)
        db.add(company)
        if dc.platform and dc.slug:
            board = CompanyATSBoard(
                company_id=company.id,
                platform=dc.platform,
                slug=dc.slug,
                is_active=True,
            )
            db.add(board)
        dc.status = "added"
        imported += 1
        # Track the slug so a duplicate slug in the SAME batch
        # (e.g. "data-co" in two discovered rows) skips the second.
        existing_slugs.add(slug)

    await db.commit()
    return {"imported": imported, "skipped": skipped}


@router.post("/companies/bulk-ignore")
async def bulk_ignore_discovered(
    body: BulkIdsRequest,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Bulk ignore discovered companies.

    F286 (closes F137 b): one batched UPDATE replaces the
    per-id SELECT-then-mutate loop. Cuts N round-trips down to 1.
    """
    # Single UPDATE filters by id-list AND status to skip rows
    # already ignored — the rowcount is exactly "rows actually
    # changed", which matches the response semantics.
    from sqlalchemy import update as sa_update
    result = await db.execute(
        sa_update(DiscoveredCompany)
        .where(
            DiscoveredCompany.id.in_(body.ids),
            DiscoveredCompany.status != "ignored",
        )
        .values(status="ignored")
    )
    await db.commit()
    return {"updated": result.rowcount or 0}
