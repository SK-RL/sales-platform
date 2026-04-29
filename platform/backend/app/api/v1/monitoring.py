"""Admin monitoring API endpoints — system health, storage, uptime, VM."""

import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job, JobDescription
from app.models.company import Company, CompanyATSBoard
from app.models.scan import ScanLog
from app.models.discovery import DiscoveryRun
from app.models.user import User
from app.models.review import Review
from app.api.deps import get_current_user, require_role
from app.services.host_stats import get_vm_metrics

# Regression finding 125: if a discovery cycle hasn't run in more than
# 2× the expected interval, the /monitoring response emits a warning-
# flagged freshness signal so ops can catch beat-schedule outages
# without shelling into the DB. Aggressive mode is the default
# production cadence; in normal (weekly) mode ops should adjust the
# expected interval in `celery_app.py::beat_schedule` — the stale
# threshold here tracks the conservative upper bound.
_DISCOVERY_AGGRESSIVE_INTERVAL_HOURS = 24  # nightly at 00:00 UTC
_DISCOVERY_STALE_MULTIPLIER = 2            # warn after 2× the interval

router = APIRouter(prefix="/monitoring", tags=["monitoring"])

# Track when the backend started
_BOOT_TIME = time.time()


@router.get("")
async def get_system_health(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get full system health overview (admin only)."""
    now = datetime.now(timezone.utc)

    # --- Database health ---
    try:
        await db.execute(text("SELECT 1"))
        db_healthy = True
    except Exception:
        db_healthy = False

    # --- Table row counts ---
    job_count = (await db.execute(select(func.count()).select_from(Job))).scalar() or 0
    company_count = (await db.execute(select(func.count()).select_from(Company))).scalar() or 0
    board_count = (await db.execute(select(func.count()).select_from(CompanyATSBoard))).scalar() or 0
    active_board_count = (await db.execute(
        select(func.count()).select_from(CompanyATSBoard).where(CompanyATSBoard.is_active.is_(True))
    )).scalar() or 0
    review_count = (await db.execute(select(func.count()).select_from(Review))).scalar() or 0
    scan_log_count = (await db.execute(select(func.count()).select_from(ScanLog))).scalar() or 0

    # --- Jobs breakdown ---
    role_clusters = (await db.execute(
        select(Job.role_cluster, func.count()).group_by(Job.role_cluster)
    )).all()
    role_cluster_counts = {(rc or "unclassified"): c for rc, c in role_clusters}

    geography_buckets = (await db.execute(
        select(Job.geography_bucket, func.count()).group_by(Job.geography_bucket)
    )).all()
    geography_counts = {(gb or "unclassified"): c for gb, c in geography_buckets}

    platforms = (await db.execute(
        select(Job.platform, func.count()).group_by(Job.platform)
    )).all()
    platform_counts = {p: c for p, c in platforms}

    status_counts_raw = (await db.execute(
        select(Job.status, func.count()).group_by(Job.status)
    )).all()
    status_counts = {s: c for s, c in status_counts_raw}

    # --- Scoring stats ---
    scored_count = (await db.execute(
        select(func.count()).select_from(Job).where(Job.relevance_score > 0)
    )).scalar() or 0
    avg_score = (await db.execute(
        select(func.avg(Job.relevance_score)).where(Job.relevance_score > 0)
    )).scalar() or 0

    # --- Recent scan activity ---
    last_24h = now - timedelta(hours=24)
    scans_24h = (await db.execute(
        select(func.count()).select_from(ScanLog).where(ScanLog.started_at >= last_24h)
    )).scalar() or 0
    new_jobs_24h = (await db.execute(
        select(func.sum(ScanLog.new_jobs)).where(ScanLog.started_at >= last_24h)
    )).scalar() or 0

    last_scan = (await db.execute(
        select(ScanLog).order_by(ScanLog.started_at.desc()).limit(1)
    )).scalar_one_or_none()

    errors_24h = (await db.execute(
        select(func.sum(ScanLog.errors)).where(ScanLog.started_at >= last_24h)
    )).scalar() or 0

    # F125: discovery-run freshness. Before this, nothing on the admin
    # monitoring page surfaced "discovery is dead" — a missed beat
    # would go unnoticed for days because the only evidence was
    # `/discovery/runs` showing no recent rows, and admins don't check
    # that without a prompt. Now we surface last_run_at + a
    # `freshness: ok|stale|never_run` flag that the frontend can flip
    # to a warning badge. Also returns `runs_24h` alongside the
    # existing `scans_run` so the two cadences are legible side-by-
    # side.
    #
    # F237(c) regression fix: filter the freshness check on
    # ``status='completed'``. Previously any fresh ``pending`` row —
    # including the orphan rows that `fix_stuck_discovery_runs` hasn't
    # swept yet — would make freshness look "ok" even when the actual
    # last *completed* discovery was days stale. Filtering on completed
    # status turns this into an honest signal. ``last_discovery_at``
    # surfaced to the UI also uses the completed row so admins don't
    # see a timestamp that has no scan payload behind it. The separate
    # ``discovery_runs_24h`` counter still counts all rows (including
    # pending/failed) — it's a throughput signal, not a success signal.
    last_discovery = (await db.execute(
        select(DiscoveryRun)
        .where(DiscoveryRun.status == "completed")
        .order_by(DiscoveryRun.started_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    discovery_runs_24h = (await db.execute(
        select(func.count()).select_from(DiscoveryRun).where(
            DiscoveryRun.started_at >= last_24h
        )
    )).scalar() or 0

    if last_discovery is None:
        discovery_freshness = "never_run"
    else:
        age = now - last_discovery.started_at.replace(tzinfo=timezone.utc)
        stale_threshold = timedelta(
            hours=_DISCOVERY_AGGRESSIVE_INTERVAL_HOURS
            * _DISCOVERY_STALE_MULTIPLIER
        )
        discovery_freshness = "stale" if age > stale_threshold else "ok"

    # --- Database size (PostgreSQL) ---
    try:
        db_size_result = await db.execute(text("SELECT pg_database_size(current_database())"))
        db_size_bytes = db_size_result.scalar() or 0
    except Exception:
        db_size_bytes = 0

    # Table sizes
    try:
        table_sizes_result = await db.execute(text("""
            SELECT relname AS table_name,
                   pg_total_relation_size(relid) AS total_bytes
            FROM pg_catalog.pg_statio_user_tables
            ORDER BY pg_total_relation_size(relid) DESC
            LIMIT 10
        """))
        table_sizes = [{"table": r[0], "size_bytes": r[1]} for r in table_sizes_result]
    except Exception:
        table_sizes = []

    # --- Uptime ---
    uptime_seconds = int(time.time() - _BOOT_TIME)

    # F234: top-level `ai_configured` flag so admins see at a glance
    # whether the ANTHROPIC_API_KEY env var made it into the running
    # container. Pre-fix, the only signal was hitting an AI endpoint
    # and reading "Contact an administrator" — that sent the wrong
    # support cycle (admins assumed the GitHub Secret was unset, when
    # really the deploy script wasn't persisting it). Now the
    # MonitoringPage can render a red badge when this is False even
    # though the deploy reported success.
    #
    # `get_settings()` factory not module-level `settings` — same
    # access pattern every other router uses (see resume.py:704).
    # Conflict resolved by taking HEAD; feat branch had the pre-F234
    # broken import that 500'd at runtime.
    from app.config import get_settings
    settings = get_settings()
    raw_key = (
        settings.anthropic_api_key.get_secret_value()
        if settings.anthropic_api_key
        else ""
    )
    ai_configured = bool(raw_key.strip())

    return {
        "timestamp": now.isoformat(),
        "uptime_seconds": uptime_seconds,
        "ai_configured": ai_configured,
        "database": {
            "healthy": db_healthy,
            "size_bytes": db_size_bytes,
            "size_mb": round(db_size_bytes / (1024 * 1024), 1),
            "table_sizes": table_sizes,
        },
        "data_counts": {
            "jobs": job_count,
            "companies": company_count,
            "boards_total": board_count,
            "boards_active": active_board_count,
            "reviews": review_count,
            "scan_logs": scan_log_count,
            "scored_jobs": scored_count,
        },
        "jobs_breakdown": {
            "by_role_cluster": role_cluster_counts,
            "by_geography": geography_counts,
            "by_platform": platform_counts,
            "by_status": status_counts,
        },
        "scoring": {
            "scored_count": scored_count,
            "unscored_count": job_count - scored_count,
            "avg_score": round(float(avg_score), 2),
        },
        "activity_24h": {
            "scans_run": scans_24h,
            "new_jobs_added": new_jobs_24h or 0,
            "errors": errors_24h,
            "last_scan_at": last_scan.started_at.isoformat() if last_scan else None,
            "last_scan_source": last_scan.source if last_scan else None,
            # F125: discovery-run freshness so admins see missed beats
            # without shelling into the DB. `freshness` drives a badge
            # in the MonitoringPage UI (ok → green, stale → yellow,
            # never_run → red). `last_discovery_at` is the actual
            # timestamp for the tooltip; `discovery_runs_24h` matches
            # the existing `scans_run` shape so both cadences are
            # legible at a glance.
            "last_discovery_at": (
                last_discovery.started_at.isoformat()
                if last_discovery
                else None
            ),
            "discovery_runs_24h": discovery_runs_24h,
            "discovery_freshness": discovery_freshness,
        },
    }


@router.get("/vm", dependencies=[Depends(require_role("admin"))])
async def get_vm_health():
    """VM host-level metrics + Oracle Always-Free guardrails (admin only).

    Data is sourced from a JSON snapshot written by the host collector
    (`/usr/local/bin/collect-host-metrics.sh`, cron every 1 min). If the
    snapshot file is absent (dev, CI), the response has `available: False`.
    """
    return get_vm_metrics()


_BACKUP_LABEL_MAX_LEN = 64


def _sanitize_backup_label(raw: str) -> str:
    """F284: Strip control bytes (NUL, CR, LF, TAB, etc.) from the
    label and trim. The label flows verbatim into ``manifest.json``
    and the access-log line; control bytes there cause log-injection
    (e.g. forged ``\\nERROR`` lines that confuse log parsers) and
    JSON corruption (a literal ``\\n`` in a value breaks
    line-delimited JSON consumers).

    Empty after strip → ``"manual"`` to keep the task contract
    happy. Per-byte filter rather than a regex so we drop any
    control char in the 0x00-0x1F range (plus 0x7F DEL).
    """
    cleaned = "".join(
        ch for ch in (raw or "")
        if 32 <= ord(ch) < 127 or ord(ch) > 159
    ).strip()
    return cleaned or "manual"


@router.post("/backup", dependencies=[Depends(require_role("admin"))])
async def trigger_backup(
    # F284 (closes F143): cap label length, sanitize control bytes,
    # acquire the "backup" scan-lock before queueing so concurrent
    # triggers don't race on ``BACKUP_ROOT/<ts>/`` directory creation
    # under multi-worker Celery (which would corrupt manifests).
    label: str = Query(default="manual", max_length=_BACKUP_LABEL_MAX_LEN),
):
    """Trigger an on-demand database backup (admin only)."""
    from app.utils.scan_lock import acquire_scan_lock
    from app.workers.tasks.backup_task import run_backup

    safe_label = _sanitize_backup_label(label)

    # Atomic lock acquisition. The task's ``finally`` block releases
    # ``scan_lock:backup`` so back-to-back manual + nightly backups
    # are possible once one finishes. TTL is the safety valve if a
    # task dies before releasing.
    if not await acquire_scan_lock("backup"):
        raise HTTPException(
            status_code=409,
            detail=(
                "A backup is already in progress. Wait for it to "
                "finish before triggering another."
            ),
        )

    task = run_backup.delay(label=safe_label)
    return {"task_id": task.id, "status": "queued", "label": safe_label}


@router.post("/reclassify-jobs", dependencies=[Depends(require_role("admin"))])
async def trigger_reclassify_jobs():
    """Re-run role matching + geography + relevance scoring on every active job.

    Regression finding 227: historical rows don't get re-classified
    when the negative-signal lists in `_role_matching.py` are
    updated (F91/F92/F227 all shipped keyword additions without a
    follow-up reclassification pass). Admins can trigger this
    endpoint post-deploy to wash stale classifications through the
    current matcher. Uses the existing
    `maintenance_task.reclassify_and_rescore` Celery task (see
    workers/tasks/maintenance_task.py:132) — the only net-new piece
    is this admin trigger surface. Admin-only because it touches
    every active Job row (~13k at writing) and runs for ~30-60
    seconds; should never be fired by unprivileged callers.
    """
    from app.workers.tasks.maintenance_task import reclassify_and_rescore
    task = reclassify_and_rescore.delay()
    return {"task_id": task.id, "status": "queued"}


@router.post("/backfill-job-descriptions", dependencies=[Depends(require_role("admin"))])
async def trigger_backfill_job_descriptions(
    batch_size: int = Query(500, ge=1, le=2000),
    max_jobs: int | None = Query(None, ge=1, le=100_000),
):
    """One-shot backfill for ``JobDescription`` rows on historical jobs.

    Regression findings 97 + 101: the scan pipeline only started
    writing ``JobDescription`` rows in a recent round. Historical
    Jobs have ``raw_json`` populated but no companion description
    row, which collapsed resume scoring into 4 distinct values
    across 600+ jobs (the scorer was reading an empty ``text_content``
    and falling back to the cluster baseline keyword bag). The
    online raw_json fallback in ``resume_score_task`` (F97) covers
    this at scoring time, but every read path that joins
    ``JobDescription`` (``/jobs/{id}/description``, the JD-text-only
    ATS test harness) still sees empty rows.

    This endpoint queues a Celery task that walks the gap once.
    Idempotent — re-runs are safe because the WHERE clause filters
    out anything that already has a description row. ``batch_size``
    controls per-commit write set; ``max_jobs`` lets ops cap a
    one-shot smoke test before running the full sweep.
    """
    from app.workers.tasks.maintenance_task import backfill_job_descriptions
    task = backfill_job_descriptions.delay(batch_size=batch_size, max_jobs=max_jobs)
    return {
        "task_id": task.id,
        "status": "queued",
        "batch_size": batch_size,
        "max_jobs": max_jobs,
    }


@router.get("/backups", dependencies=[Depends(require_role("admin"))])
async def list_backups():
    """List available backups with manifest metadata."""
    import json
    from pathlib import Path
    backup_root = Path("/app/backups")
    if not backup_root.exists():
        return {"backups": []}
    items = []
    for d in sorted(backup_root.iterdir(), reverse=True):
        if not d.is_dir() or not d.name[0].isdigit():
            continue
        manifest_path = d / "manifest.json"
        entry: dict = {"timestamp": d.name, "path": str(d)}
        if manifest_path.exists():
            with open(manifest_path) as f:
                m = json.load(f)
            entry.update({
                "created_at":       m.get("created_at"),
                "label":            m.get("label", ""),
                "alembic_revision": m.get("alembic_revision"),
                "total_rows":       m.get("total_rows", 0),
                "dump_size_mb":     round(m.get("dump_size_bytes", 0) / 1024 / 1024, 2),
            })
        items.append(entry)
    return {"backups": items, "count": len(items)}


@router.get("/scan-errors", dependencies=[Depends(require_role("admin"))])
async def list_scan_errors(
    # Regression finding 193: `days` was a bare `int = 7` with no
    # bounds, so `?days=1000000` produced a timestamp ~2735 BC that
    # Postgres' `timestamp with time zone` range rejects, bubbling a
    # DBAPIError back as an unhelpful 500. `?days=-5` and `?days=0`
    # also silently passed through and returned an empty list, same
    # F179 pattern. Bound to [1, 3650] (10 years of history is the
    # absolute ceiling for scan-error retention we care about — the
    # real rollup window is ~7 days) so FastAPI 422s both overflows
    # and non-positive inputs at parse time with a structured error
    # message.
    days: int = Query(7, ge=1, le=3650),
    db: AsyncSession = Depends(get_db),
):
    """Regression finding 104: surface per-error detail from ScanLog.

    Returns the most recent scan logs where `errors > 0` or
    `error_message != ''`, grouped by platform/source, so admins can
    diagnose silently-failing boards without tailing container logs.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(ScanLog)
        .where(ScanLog.started_at >= cutoff)
        .where((ScanLog.errors > 0) | (ScanLog.error_message != ""))
        .order_by(ScanLog.started_at.desc())
        .limit(200)
    )
    errors = result.scalars().all()

    items = []
    for e in errors:
        items.append({
            "id": str(e.id),
            "platform": e.platform,
            "source": e.source,
            "started_at": e.started_at.isoformat(),
            "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            "errors": e.errors,
            "error_message": e.error_message,
            "jobs_found": e.jobs_found,
            "duration_ms": e.duration_ms,
        })

    # Summary by platform
    by_platform: dict[str, int] = {}
    for item in items:
        by_platform[item["platform"]] = by_platform.get(item["platform"], 0) + 1

    return {
        "items": items,
        "total": len(items),
        "by_platform": by_platform,
        "days": days,
    }
