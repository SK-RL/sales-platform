"""Platform monitoring API endpoints."""

from typing import get_args
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.company import CompanyATSBoard
from app.models.scan import ScanLog
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.utils.company_name import looks_like_junk_company_name
from app.utils.sanitize import strip_html_tags
from app.utils.scan_lock import acquire_scan_lock, release_scan_lock


# F287 (closes F147 a-d) — Pydantic schema for POST /boards.
#
# Pre-fix the handler took ``body: dict`` and pulled fields with
# ``body.get(...)`` directly. That meant:
#   (a) ``extra="forbid"`` couldn't fire — extra fields silently
#       dropped (F128 pattern).
#   (b) ``company_name`` had no length cap — 5KB names persisted
#       and crashed the underlying Postgres ``String(N)`` column
#       writer with HTTP 500.
#   (c) HTML in ``company_name`` flowed through to the DB and
#       rendered verbatim in the admin UI (stored XSS).
#   (d) ``slug``/``platform`` had no validation — though platform
#       was at least allow-listed in the handler body.
# Re-shaping as a real Pydantic model lets the schema layer reject
# bad input at parse time, and ``strip_html_tags`` on company_name
# closes the XSS vector at the only chokepoint.
class _BoardCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(..., min_length=1, max_length=200)
    platform: str = Field(..., min_length=1, max_length=50)
    slug: str = Field(..., min_length=1, max_length=200)

    @field_validator("company_name")
    @classmethod
    def _strip_html_company_name(cls, v: str) -> str:
        # F147(c): strict-strip rather than sanitize — display names
        # have no legitimate HTML use. Same helper feedback uses
        # (F162) and role-cluster display_name uses (F285).
        return strip_html_tags(v).strip()

# Regression finding 191 (re-exported from schemas.job in F218): `?platform=`
# on the list endpoints wasn't validated, so typos (`GREENHOUSE`,
# `grenhouse`) silently returned `{"items":[],"total":0}` — same F128 /
# F162 pattern. The platform set is fixed in code (BaseFetcher subclasses
# in app/fetchers/) so Literal is the right tool. Moved the definition to
# `schemas/job.py` so jobs.py can reuse the same Literal rather than
# declaring a parallel one that could drift.
from app.schemas.job import PlatformFilter  # noqa: F401 (re-exported)

router = APIRouter(prefix="/platforms", tags=["platforms"])


@router.get("")
async def list_platforms(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get overview of all monitored platforms with stats."""
    # Count boards per platform
    boards_q = (
        select(
            CompanyATSBoard.platform,
            func.count(CompanyATSBoard.id).label("total_boards"),
            func.sum(case((CompanyATSBoard.is_active == True, 1), else_=0)).label("active_boards"),
        )
        .group_by(CompanyATSBoard.platform)
    )
    boards_result = await db.execute(boards_q)
    boards_by_platform = {row.platform: {"total_boards": row.total_boards, "active_boards": int(row.active_boards or 0)} for row in boards_result}

    # Count jobs per platform
    jobs_q = (
        select(
            Job.platform,
            func.count(Job.id).label("total_jobs"),
            func.sum(case((Job.status == "new", 1), else_=0)).label("new_jobs"),
            func.sum(case((Job.status == "accepted", 1), else_=0)).label("accepted_jobs"),
            func.sum(case((Job.status == "rejected", 1), else_=0)).label("rejected_jobs"),
            func.avg(Job.relevance_score).label("avg_score"),
        )
        .group_by(Job.platform)
    )
    jobs_result = await db.execute(jobs_q)
    jobs_by_platform = {}
    for row in jobs_result:
        jobs_by_platform[row.platform] = {
            "total_jobs": row.total_jobs,
            "new_jobs": int(row.new_jobs or 0),
            "accepted_jobs": int(row.accepted_jobs or 0),
            "rejected_jobs": int(row.rejected_jobs or 0),
            "avg_score": round(float(row.avg_score or 0), 1),
        }

    # Latest scan per platform — aggregate (max completed_at + total
    # errors) PLUS the per-row stats of the single most recent run so the
    # Platforms UI can show "175 found, 0 new (12 min ago)" inline on each
    # card without a second per-platform round-trip. Two queries here:
    #   1. ``scans_q`` — aggregate max-completed_at + sum-errors per platform.
    #   2. ``last_run_q`` — DISTINCT ON-style window: for each platform,
    #      pick the row whose ``started_at`` is the most recent and pull
    #      its individual stats forward. SQLAlchemy doesn't have a direct
    #      ``DISTINCT ON`` helper for cross-dialect, so we use a
    #      row_number() window function which works on Postgres + SQLite.
    scans_q = (
        select(
            ScanLog.platform,
            func.max(ScanLog.completed_at).label("last_scan"),
            func.sum(ScanLog.errors).label("total_errors"),
        )
        .group_by(ScanLog.platform)
    )
    scans_result = await db.execute(scans_q)
    scans_by_platform = {}
    for row in scans_result:
        scans_by_platform[row.platform] = {
            "last_scan": row.last_scan.isoformat() if row.last_scan else None,
            "total_errors": int(row.total_errors or 0),
        }

    # F250: per-platform "last run" stats. Window-rank by started_at DESC
    # within each platform, then keep rank=1. Cheap on the indexed
    # (platform, started_at) compound; ~30ms even on a 250k-row scan_logs
    # table. We also pull error_message for the tooltip on the failure
    # icon, so admins can see "what went wrong on the last try" without
    # expanding the Scan Logs panel.
    rn = func.row_number().over(
        partition_by=ScanLog.platform,
        order_by=ScanLog.started_at.desc(),
    ).label("rn")
    ranked = (
        select(
            ScanLog.platform,
            ScanLog.source,
            ScanLog.started_at,
            ScanLog.completed_at,
            ScanLog.jobs_found,
            ScanLog.new_jobs,
            ScanLog.updated_jobs,
            ScanLog.errors,
            ScanLog.error_message,
            rn,
        )
        .subquery()
    )
    last_run_q = select(ranked).where(ranked.c.rn == 1)
    last_run_result = await db.execute(last_run_q)
    last_run_by_platform: dict[str, dict] = {}
    for row in last_run_result:
        last_run_by_platform[row.platform] = {
            "source": row.source or "",
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "jobs_found": int(row.jobs_found or 0),
            "new_jobs": int(row.new_jobs or 0),
            "updated_jobs": int(row.updated_jobs or 0),
            "errors": int(row.errors or 0),
            "error_message": row.error_message or "",
        }

    # Combine into platform list
    all_platforms = set(boards_by_platform.keys()) | set(jobs_by_platform.keys())
    platforms = []
    for name in sorted(all_platforms):
        boards = boards_by_platform.get(name, {"total_boards": 0, "active_boards": 0})
        jobs = jobs_by_platform.get(name, {"total_jobs": 0, "new_jobs": 0, "accepted_jobs": 0, "rejected_jobs": 0, "avg_score": 0})
        scans = scans_by_platform.get(name, {"last_scan": None, "total_errors": 0})
        last_run = last_run_by_platform.get(name)
        platforms.append({
            "name": name,
            **boards,
            **jobs,
            **scans,
            # F250: per-platform last-run snapshot. Null when the
            # platform has zero scan_logs rows (newly seeded boards,
            # never run). Frontend uses this to show "175 found, 0 new"
            # under the Last-scan timestamp on each card.
            "last_run": last_run,
        })

    return {"platforms": platforms}


@router.get("/boards")
async def list_boards(
    # F191: Literal-typed so FastAPI 422s typos like `GREENHOUSE`
    # (wrong case) or `grenhouse` (typo) instead of silently returning
    # an empty list.
    platform: PlatformFilter | None = None,
    # Regression finding 223: previously this handler dumped the ENTIRE
    # 871-row board registry (204,890 bytes) on every authenticated GET
    # with no pagination, no search filter, and no role guard. Same
    # "unbounded list + envelope drift" pair as F217 on scan-logs, but
    # with the additional info-disclosure angle: boards reveal which
    # company ATS slugs we scrape, which is internal ops data that
    # viewer/reviewer roles shouldn't have a wholesale view of. Fix
    # mirrors F217:
    #   (a) `page` / `page_size` with ge/le bounds — page_size default
    #       500 keeps the existing "one platform at a time" UX intact
    #       (largest platform ~200 boards today) while bounding the
    #       worst-case response at 1000 rows;
    #   (b) optional `search` term (ilike on company name OR slug) so
    #       the PlatformsPage "find by company" case stops requiring a
    #       full-registry pull filtered client-side;
    #   (c) canonical envelope `{items,total,page,page_size,total_pages}`
    #       (was `{items,total}`) to match F108/F205/F212/F217/F220(A);
    #   (d) `require_role("admin")` so viewer/reviewer roles 403 instead
    #       of receiving the full internal scraping registry. The
    #       downstream toggle/add/delete handlers are already admin-only,
    #       so this closes the read-vs-write permission gap. Frontend
    #       "Boards" expand button is hidden for non-admins in the same
    #       round (mirroring the F217 Scan Logs pattern).
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=1000),
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """List ATS boards with company info (admin-only, paginated)."""
    from app.models.company import Company
    from sqlalchemy.orm import joinedload

    count_q = select(func.count(CompanyATSBoard.id))
    query = select(CompanyATSBoard).options(joinedload(CompanyATSBoard.company))
    if platform:
        query = query.where(CompanyATSBoard.platform == platform)
        count_q = count_q.where(CompanyATSBoard.platform == platform)
    if search:
        # Join once for the ILIKE against company.name; CompanyATSBoard.slug
        # is on the table itself so no join needed for that side of the OR.
        from sqlalchemy import or_
        term = f"%{search.strip()}%"
        query = query.join(Company, CompanyATSBoard.company_id == Company.id).where(
            or_(Company.name.ilike(term), CompanyATSBoard.slug.ilike(term))
        )
        count_q = count_q.join(Company, CompanyATSBoard.company_id == Company.id).where(
            or_(Company.name.ilike(term), CompanyATSBoard.slug.ilike(term))
        )
    query = query.order_by(CompanyATSBoard.platform, CompanyATSBoard.slug)
    query = query.offset((page - 1) * page_size).limit(page_size)

    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(query)
    boards = result.unique().scalars().all()

    return {
        "items": [
            {
                "id": str(b.id),
                "company_id": str(b.company_id),
                "company_name": b.company.name if b.company else "Unknown",
                "platform": b.platform,
                "slug": b.slug,
                "is_active": b.is_active,
                "last_scanned_at": b.last_scanned_at.isoformat() if b.last_scanned_at else None,
            }
            for b in boards
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 0,
    }


@router.post("/boards/{board_id}/toggle")
async def toggle_board(
    board_id: UUID,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable an ATS board."""
    result = await db.execute(select(CompanyATSBoard).where(CompanyATSBoard.id == board_id))
    board = result.scalar_one_or_none()
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")
    board.is_active = not board.is_active
    await db.commit()
    return {"id": str(board.id), "is_active": board.is_active}


@router.post("/boards")
async def add_board(
    body: _BoardCreateBody,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Add a new ATS board. Requires company_name, platform, slug."""
    from app.models.company import Company
    import uuid as _uuid
    from datetime import datetime, timezone

    # F287: schema validation already enforced presence + length +
    # strip_html_tags on company_name. Local trim + lowercase on
    # platform stays — the platform allow-list check below relies on
    # the canonical lowercased form.
    company_name = body.company_name
    platform = body.platform.strip().lower()
    slug = body.slug.strip()

    # F246(b) follow-up: read the allow-list from the canonical
    # ``FETCHER_MAP`` rather than hardcoding it here. Pre-fix, this
    # list lagged the FETCHER_MAP additions every time a new fetcher
    # shipped — Workday, weworkremotely, remoteok, remotive, linkedin,
    # hackernews, yc_waas all silently couldn't be added via the admin
    # UI even though the workers could fetch them. Single source of
    # truth means a future fetcher addition wires up immediately.
    from app.fetchers import FETCHER_MAP
    valid_platforms = sorted(FETCHER_MAP.keys())
    if platform not in valid_platforms:
        raise HTTPException(status_code=400, detail=f"platform must be one of: {', '.join(valid_platforms)}")

    # Regression finding 37: reject LinkedIn-hashtag / staffing / scratch
    # names up-front so manual admin adds can't reintroduce the same
    # junk the ingest filter (scan_task.py) drops automatically.
    if looks_like_junk_company_name(company_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "company_name looks like a scraping artifact (hashtag harvest, "
                "staffing shell, purely numeric, or scratch string). If this "
                "is a real company, please pass a cleaned display name."
            ),
        )

    # Find or create company
    result = await db.execute(select(Company).where(Company.name == company_name))
    company = result.scalar_one_or_none()
    if not company:
        company = Company(
            id=_uuid.uuid4(),
            name=company_name,
            slug=company_name.lower().replace(" ", "-").replace(".", "-"),
            is_target=False,
            created_at=datetime.now(timezone.utc),
        )
        db.add(company)
        await db.flush()

    # Check for duplicate board
    result = await db.execute(
        select(CompanyATSBoard).where(
            CompanyATSBoard.company_id == company.id,
            CompanyATSBoard.platform == platform,
            CompanyATSBoard.slug == slug,
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Board already exists")

    board = CompanyATSBoard(
        id=_uuid.uuid4(),
        company_id=company.id,
        platform=platform,
        slug=slug,
        is_active=True,
    )
    db.add(board)
    await db.commit()

    return {
        "id": str(board.id),
        "company_id": str(company.id),
        "company_name": company.name,
        "platform": board.platform,
        "slug": board.slug,
        "is_active": True,
        "last_scanned_at": None,
    }


@router.delete("/boards/{board_id}")
async def delete_board(
    board_id: UUID,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Delete an ATS board."""
    result = await db.execute(select(CompanyATSBoard).where(CompanyATSBoard.id == board_id))
    board = result.scalar_one_or_none()
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")
    await db.delete(board)
    await db.commit()
    return {"ok": True}


@router.post("/scan/all")
async def trigger_full_scan(
    user: User = Depends(require_role("admin")),
):
    """Trigger a full scan of all active platforms.

    Regression finding 82: double-click was queueing two tasks that
    each iterated 871 boards. Redis `SET NX EX` lock now dedups at
    the endpoint, returning 409 if a full scan is already in flight.
    """
    from app.workers.tasks.scan_task import scan_all_platforms

    if not await acquire_scan_lock("all"):
        raise HTTPException(
            status_code=409,
            detail="A full scan is already running. Wait for it to complete or check /scan/status.",
        )
    try:
        task = scan_all_platforms.delay()
    except Exception:
        # `.delay()` pushes to Redis; if that fails we own the lock
        # but never kicked off work — release so the admin can retry.
        release_scan_lock("all")
        raise
    return {"task_id": str(task.id), "status": "queued", "scope": "all_platforms"}


@router.post("/scan/{platform}")
async def trigger_platform_scan(
    platform: str,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a scan for a specific platform only."""
    from app.workers.tasks.scan_task import scan_platform
    # Regression finding 118: previously this hardcoded a 10-platform
    # whitelist that had drifted behind the schema — linkedin (1,644
    # jobs), weworkremotely (386), remoteok (189), and remotive (25)
    # had fetchers, had active boards, and had live rows in
    # Job.platform, but `POST /scan/linkedin` returned 400. An admin
    # could never trigger a targeted re-scan of 2,244 jobs of data
    # across 4 platforms. Fix: derive the whitelist from the
    # `PlatformFilter` Literal (which already covers all 14 known
    # fetchers — F191 docs that tuple is the single source of truth,
    # aligned with the `PLATFORM` class attribute on each `BaseFetcher`
    # subclass in `app/fetchers/`). When a new fetcher is added, updating
    # `schemas/job.py:PlatformFilter` now flows to every consumer: the
    # `?platform=` filter on /jobs, /platforms, /scan-logs, AND this
    # per-platform scan trigger — no more hunting for stale lists.
    valid_platforms = list(get_args(PlatformFilter))
    if platform not in valid_platforms:
        raise HTTPException(status_code=400, detail=f"Platform must be one of: {', '.join(valid_platforms)}")

    # Verify there are active boards for this platform
    board_count = (await db.execute(
        select(func.count()).select_from(CompanyATSBoard).where(
            CompanyATSBoard.platform == platform,
            CompanyATSBoard.is_active.is_(True),
        )
    )).scalar() or 0
    if board_count == 0:
        raise HTTPException(status_code=400, detail=f"No active boards for platform: {platform}")

    scope = f"platform:{platform}"
    if not await acquire_scan_lock(scope):
        raise HTTPException(
            status_code=409,
            detail=f"A scan of {platform} is already running.",
        )
    try:
        task = scan_platform.delay(platform)
    except Exception:
        release_scan_lock(scope)
        raise
    return {"task_id": str(task.id), "status": "queued", "platform": platform, "boards": board_count}


@router.post("/scan/board/{board_id}")
async def trigger_board_scan(
    board_id: UUID,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a scan for a single ATS board."""
    from app.workers.tasks.scan_task import scan_single_board
    result = await db.execute(select(CompanyATSBoard).where(CompanyATSBoard.id == board_id))
    board = result.scalar_one_or_none()
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")

    scope = f"board:{board_id}"
    if not await acquire_scan_lock(scope):
        raise HTTPException(
            status_code=409,
            detail=f"A scan of {board.platform}/{board.slug} is already running.",
        )
    try:
        task = scan_single_board.delay(str(board_id))
    except Exception:
        release_scan_lock(scope)
        raise
    return {"task_id": str(task.id), "status": "queued", "board": board.slug, "platform": board.platform}


@router.post("/scan/discover")
async def trigger_discovery_scan(
    user: User = Depends(require_role("admin")),
):
    """Trigger platform discovery: find new ATS boards and auto-add them."""
    from app.workers.tasks.discovery_task import discover_and_add_boards

    if not await acquire_scan_lock("discover"):
        raise HTTPException(
            status_code=409,
            detail="A discovery scan is already running.",
        )
    try:
        # F237(a): tag admin-initiated discovery runs with
        # ``source="manual"`` so they're distinguishable from the
        # beat-scheduled rows (which default to ``"scheduled"``).
        # Without this, the admin UI and the scheduler run would
        # both tag their rows the same way and the freshness /
        # history monitoring can't tell them apart.
        task = discover_and_add_boards.delay(source="manual")
    except Exception:
        release_scan_lock("discover")
        raise
    return {"task_id": str(task.id), "status": "queued", "scope": "platform_discovery"}


@router.get("/scan/status/{task_id}")
async def get_scan_status(
    # Regression finding 190: previously `task_id: str` so callers
    # could pass any garbage string ("not-a-real-task", SQL fragments,
    # empty-string path segment) and Celery's default behaviour for
    # unknown ids is `PENDING` — indistinguishable from a real queued
    # task. A frontend polling on a dropped task_id would spin
    # forever. Typing the param as UUID kicks non-UUID inputs out
    # with a structured 422 at parse time; legitimate task_ids are
    # always UUIDs (every scan endpoint returns `str(task.id)` from
    # Celery, which uses uuid4 by default).
    task_id: UUID,
    user: User = Depends(require_role("admin")),
):
    """Check the status of a scan task."""
    from app.workers.celery_app import celery_app
    task_id_str = str(task_id)
    result = celery_app.AsyncResult(task_id_str)
    response = {
        "task_id": task_id_str,
        "status": result.status,
    }
    if result.ready():
        response["result"] = result.result if result.successful() else str(result.result)
    return response


@router.get("/scan-logs")
async def get_scan_logs(
    # F191: same validation as /boards — typos no longer return empty.
    platform: PlatformFilter | None = None,
    # Regression finding 217: three distinct bugs in the previous version
    # were fixed together because they compound:
    #   (a) `limit: int = 50` had no `ge`/`le` bounds, so `?limit=-1` got
    #       passed to PG which 500'd on `LIMIT -1`;
    #   (b) `?limit=999999` returned 68.7 MB / 236,906 rows on any
    #       authenticated JWT — a trivial DoS lever and a data-leak
    #       (error_message rows sometimes contain source IPs and stack
    #       fragments from fetcher failures);
    #   (c) the response envelope was bare `{"items":[...]}` with no
    #       `total`/`page`/`page_size`/`total_pages`, so the PlatformsPage
    #       "Recent Scan Logs" panel had no way to page back past 50.
    # Fix mirrors the F179 /analytics/trends bounding + the F205/F212/F220(A)
    # canonical pagination envelope. Admin gate matches the CLAUDE.md role
    # hierarchy (scan logs = "scan controls" = admin-only per the spec);
    # the frontend "Scan Logs" button is hidden for non-admins in the same
    # round so a viewer role doesn't see a button that 403s.
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get recent scan logs (admin-only)."""
    count_q = select(func.count(ScanLog.id))
    if platform:
        count_q = count_q.where(ScanLog.platform == platform)
    total = (await db.execute(count_q)).scalar() or 0

    query = select(ScanLog).order_by(ScanLog.started_at.desc())
    if platform:
        query = query.where(ScanLog.platform == platform)
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    logs = result.scalars().all()

    return {
        "items": [
            {
                "id": str(l.id),
                "source": l.source,
                "platform": l.platform,
                "started_at": l.started_at.isoformat() if l.started_at else None,
                "completed_at": l.completed_at.isoformat() if l.completed_at else None,
                "jobs_found": l.jobs_found,
                "new_jobs": l.new_jobs,
                "updated_jobs": l.updated_jobs,
                "errors": l.errors,
                "error_message": l.error_message,
                "duration_ms": l.duration_ms,
            }
            for l in logs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 0,
    }
