"""Maintenance tasks -- job expiration, re-scoring, and data quality."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, func

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession
from app.workers.tasks._scoring import compute_relevance_score
from app.workers.tasks._role_matching import (
    match_role_with_config, classify_geography, load_cluster_config_sync,
)
from app.models.company import Company
from app.models.job import Job, JobDescription
from app.models.scoring_signal import ScoringSignal
from app.models.discovery import DiscoveryRun
# F272 — ScanLog needed by prune_scan_logs. Live verification post-
# F272(b) caught a NameError("name 'ScanLog' is not defined") on the
# very first invocation; the task body referenced ``ScanLog`` but
# the import was never added. Catching this here so the next
# pre-commit run on a clean checkout would fail loudly instead of
# at celery-worker runtime.
from app.models.scan import ScanLog
from app.workers.tasks._feedback import get_feedback_adjustment
from app.utils.job_description import extract_description

logger = logging.getLogger(__name__)

# Jobs not seen in this many days are marked expired
STALE_THRESHOLD_DAYS = 14


@celery_app.task(name="app.workers.tasks.maintenance_task.expire_stale_jobs")
def expire_stale_jobs():
    """Mark jobs not seen in the last 14 days as expired."""
    logger.info("Starting expire_stale_jobs")
    session = SyncSession()

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_THRESHOLD_DAYS)

        result = session.execute(
            update(Job)
            .where(
                Job.status.in_(["new", "under_review"]),
                Job.last_seen_at < cutoff,
            )
            .values(
                status="expired",
                expired_at=datetime.now(timezone.utc),
            )
        )
        expired_count = result.rowcount
        session.commit()

        logger.info("expire_stale_jobs complete: %d jobs expired", expired_count)
        return {"expired": expired_count}

    except Exception as e:
        logger.exception("expire_stale_jobs failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()


# F272 — scan_logs retention. Manual integrity audit found the
# scan_logs table at 252k rows with no cleanup task wired up. The
# table grows ~13k rows/day (16 platforms × ~800 scans/day) so at
# current rate the row count doubles every ~20 days unbounded.
# Concrete impact today is small (~38MB on disk, queries still
# fast on the started_at btree index), but at 1M+ rows the
# /monitoring activity_24h aggregate query starts paying real cost.
# Better to add the prune task now than firefight at 5M rows later.
#
# Retention window: 60 days. Rationale:
#   * /monitoring activity_24h only looks at last 24h — 60 days is
#     vastly beyond what's queried.
#   * /platforms last_scan / total_errors aggregations look at
#     "most recent" or "last 24h" — same reasoning.
#   * Debug cases occasionally need to look at "what happened a
#     month ago when scans started failing" — 60 days covers two
#     months of cycles which is a reasonable forensic window.
#   * If the team needs longer retention later, bump the constant
#     and re-deploy; the historical rows are compacted in postgres
#     so re-shrinking is fast.
SCAN_LOG_RETENTION_DAYS = 60


@celery_app.task(name="app.workers.tasks.maintenance_task.prune_scan_logs")
def prune_scan_logs():
    """Delete scan_logs rows older than ``SCAN_LOG_RETENTION_DAYS``.

    Single bulk DELETE — postgres handles 100k+ row deletes in
    seconds against the started_at btree index. No chunking needed
    at current scale; if the table grows past 10M rows we should
    revisit and chunk this like F262 does for rescore_jobs.

    F272(d) — original design preserved the most-recent row per
    (platform, source) via ``func.max(ScanLog.id) GROUP BY platform,
    source``. That fails on Postgres because ``ScanLog.id`` is a
    UUID column and ``max(uuid)`` is not a defined aggregate. The
    earlier per-key preservation was also a "nice to have" rather
    than load-bearing — boards scanned within the last 60 days are
    automatically preserved (they're after the cutoff), and boards
    that haven't been scanned in 60+ days are typically dead/
    inactive anyway. The /platforms aggregate just shows fewer
    historical rows but the data is still correct.

    Simplified implementation: single DELETE WHERE started_at < cutoff.
    No subquery, no UUID aggregate, no preservation edge cases.
    """
    logger.info("Starting prune_scan_logs (retention=%dd)", SCAN_LOG_RETENTION_DAYS)
    session = SyncSession()

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=SCAN_LOG_RETENTION_DAYS)
        result = session.execute(
            ScanLog.__table__.delete().where(
                ScanLog.started_at < cutoff,
            )
        )
        deleted = result.rowcount
        session.commit()
        logger.info(
            "prune_scan_logs complete: %d rows deleted (retention=%dd)",
            deleted, SCAN_LOG_RETENTION_DAYS,
        )
        return {"deleted": deleted}

    except Exception as e:
        logger.exception("prune_scan_logs failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()


# F262 — chunk size for the streaming rescore loop. Empirically chosen
# to keep peak ORM identity-map RSS under ~50MB per chunk while still
# amortising the round-trip cost of the SELECT. At 86k active jobs and
# 2k chunk size that's ~43 chunks per nightly run; each chunk commits
# + ``expire_all()``s before moving on so the worker's resident memory
# stays flat instead of accumulating to ~500MB (which is what was
# OOM-killing the celery container at 03:01 UTC every night before
# this fix landed — see dmesg, Apr 25-29 2026).
_RESCORE_CHUNK = 2000


@celery_app.task(name="app.workers.tasks.maintenance_task.rescore_jobs")
def rescore_jobs():
    """Recalculate relevance_score for all active (non-expired, non-archived) jobs.

    F262: streams jobs in 2k-row chunks instead of loading everything
    into memory at once. Pre-fix, ``select(Job).all()`` materialised
    all ~86k active jobs as ORM objects in one go (~500 MB), which the
    1 GB celery container OOM-killed every night at 03:01 UTC. Now
    memory is bounded to one chunk × 2k rows regardless of catalog
    size — the task scales linearly in time with row count but stays
    flat in memory.
    """
    logger.info("Starting rescore_jobs (chunked)")
    session = SyncSession()

    try:
        active_statuses = ["new", "under_review", "accepted"]

        # Pre-load lookup tables ONCE — these are small and don't grow
        # with job count. Stored as plain Python dicts (not ORM objects)
        # so ``session.expire_all()`` between chunks doesn't invalidate
        # them. The pre-fix code held full Company ORM rows here; we
        # only need ``is_target``, so we project to a (uuid → bool)
        # dict and shed the ORM weight.
        target_lookup: dict = {
            cid: bool(is_target)
            for cid, is_target in session.execute(
                select(Company.id, Company.is_target)
            ).all()
        }

        signal_rows = session.execute(select(ScoringSignal)).scalars().all()
        signals_cache = {s.signal_key: s.weight for s in signal_rows}

        from app.workers.tasks._role_matching import load_cluster_config_sync
        cluster_config = load_cluster_config_sync(session)
        approved_roles_set = set()
        for cfg in cluster_config.values():
            for role in cfg["approved_roles"]:
                approved_roles_set.add(role.lower())

        # Stream over jobs using keyset pagination on ``id``. Keyset
        # (``WHERE id > last_id``) over OFFSET because:
        #   * it's robust to concurrent inserts during the run — new
        #     rows just get higher ids and are picked up on a future
        #     chunk or next nightly invocation;
        #   * it doesn't degrade as the run progresses (OFFSET 80,000
        #     scans 80k rows each time on Postgres before returning);
        #   * Job.id is a UUID with btree-comparable ordering, so the
        #     ``id > last_id`` predicate is index-backed.
        last_id = None
        total_checked = 0
        total_rescored = 0
        chunk_no = 0
        while True:
            q = select(Job).where(Job.status.in_(active_statuses))
            if last_id is not None:
                q = q.where(Job.id > last_id)
            batch = session.execute(
                q.order_by(Job.id).limit(_RESCORE_CHUNK)
            ).scalars().all()
            if not batch:
                break

            chunk_no += 1
            chunk_rescored = 0
            for job in batch:
                is_target = target_lookup.get(job.company_id, False)
                feedback_adj = get_feedback_adjustment(job, signals_cache)

                new_score = compute_relevance_score(
                    title=job.title,
                    matched_role=job.matched_role,
                    role_cluster=job.role_cluster,
                    is_target=is_target,
                    geography_bucket=job.geography_bucket,
                    remote_scope=job.remote_scope,
                    platform=job.platform,
                    posted_at=job.posted_at,
                    approved_roles_set=approved_roles_set if approved_roles_set else None,
                    feedback_adjustment=feedback_adj,
                )

                if job.relevance_score != new_score:
                    job.relevance_score = new_score
                    chunk_rescored += 1

            # Capture the boundary id BEFORE expire_all() invalidates
            # the ORM identity map — after expire we can't access
            # ``batch[-1].id`` without a re-fetch.
            new_last_id = batch[-1].id
            session.commit()
            # Drop ORM identity-map state so the next chunk doesn't
            # accumulate. This is the critical line — without it the
            # session retains every Job processed and we're back to
            # the pre-fix unbounded memory shape.
            session.expire_all()

            total_checked += len(batch)
            total_rescored += chunk_rescored
            last_id = new_last_id

            if chunk_no % 10 == 0:
                logger.info(
                    "rescore_jobs progress: chunk %d, %d checked, %d rescored so far",
                    chunk_no, total_checked, total_rescored,
                )

        logger.info(
            "rescore_jobs complete: %d chunks, %d active jobs checked, %d rescored",
            chunk_no, total_checked, total_rescored,
        )
        return {"checked": total_checked, "rescored": total_rescored, "chunks": chunk_no}

    except Exception as e:
        logger.exception("rescore_jobs failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.maintenance_task.reclassify_and_rescore")
def reclassify_and_rescore():
    """Re-run role matching, geography classification, and scoring for all active jobs.

    Unlike rescore_jobs which only recalculates scores, this re-runs the
    full classification pipeline with updated keywords/roles/geography
    signals.

    F262: same chunked-iteration pattern as ``rescore_jobs``. This task
    is admin-triggered (button on the monitoring page) rather than
    cron-scheduled, but the unbounded ``select(Job).all()`` was the
    same OOM hazard waiting for someone to click it. Fixing both at
    once so neither path can blow up the heavy worker.
    """
    logger.info("Starting reclassify_and_rescore (chunked)")
    session = SyncSession()

    try:
        cluster_config = load_cluster_config_sync(session)

        active_statuses = ["new", "under_review", "accepted"]

        # Pre-load lookup tables ONCE — same pattern as rescore_jobs.
        # Plain dict so ``expire_all()`` between chunks doesn't bite.
        target_lookup: dict = {
            cid: bool(is_target)
            for cid, is_target in session.execute(
                select(Company.id, Company.is_target)
            ).all()
        }

        approved_roles_set = set()
        for cfg in cluster_config.values():
            for role in cfg["approved_roles"]:
                approved_roles_set.add(role.lower())

        signal_rows = session.execute(select(ScoringSignal)).scalars().all()
        signals_cache = {s.signal_key: s.weight for s in signal_rows}

        last_id = None
        total_checked = 0
        total_reclassified = 0
        total_rescored = 0
        chunk_no = 0
        while True:
            q = select(Job).where(Job.status.in_(active_statuses))
            if last_id is not None:
                q = q.where(Job.id > last_id)
            batch = session.execute(
                q.order_by(Job.id).limit(_RESCORE_CHUNK)
            ).scalars().all()
            if not batch:
                break

            chunk_no += 1
            chunk_reclassified = 0
            chunk_rescored = 0
            for job in batch:
                # Re-run role matching
                role_match = match_role_with_config(job.title, cluster_config)
                new_cluster = role_match["role_cluster"]
                new_matched_role = role_match["matched_role"]
                new_title_norm = role_match["title_normalized"]

                # Re-run geography + remote-policy classification.
                # Both columns are updated together — the legacy
                # ``geography_bucket`` is derived from the new
                # ``(policy, countries)`` pair via ``legacy_bucket_for``
                # so they can never diverge.
                from app.workers.tasks._role_matching import classify_remote_policy
                from app.utils.remote_policy import legacy_bucket_for, normalise_countries

                new_policy, new_policy_countries = classify_remote_policy(
                    job.location_raw or "", job.remote_scope or ""
                )
                new_policy_countries = normalise_countries(new_policy_countries)
                new_geo = legacy_bucket_for(new_policy, new_policy_countries)

                cluster_changed = new_cluster != (job.role_cluster or "")
                geo_changed = new_geo != (job.geography_bucket or "")
                policy_changed = new_policy != (job.remote_policy or "unknown")
                countries_changed = list(new_policy_countries) != list(
                    job.remote_policy_countries or []
                )

                if cluster_changed or geo_changed or policy_changed or countries_changed:
                    job.role_cluster = new_cluster
                    job.matched_role = new_matched_role
                    job.title_normalized = new_title_norm
                    job.geography_bucket = new_geo
                    job.remote_policy = new_policy
                    job.remote_policy_countries = new_policy_countries
                    chunk_reclassified += 1

                # Rescore using the (possibly updated) classification
                is_target = target_lookup.get(job.company_id, False)
                feedback_adj = get_feedback_adjustment(job, signals_cache)

                new_score = compute_relevance_score(
                    title=job.title,
                    matched_role=new_matched_role,
                    role_cluster=new_cluster,
                    is_target=is_target,
                    geography_bucket=new_geo,
                    remote_scope=job.remote_scope,
                    platform=job.platform,
                    posted_at=job.posted_at,
                    approved_roles_set=approved_roles_set if approved_roles_set else None,
                    feedback_adjustment=feedback_adj,
                )

                if job.relevance_score != new_score:
                    job.relevance_score = new_score
                    chunk_rescored += 1

            new_last_id = batch[-1].id
            session.commit()
            session.expire_all()

            total_checked += len(batch)
            total_reclassified += chunk_reclassified
            total_rescored += chunk_rescored
            last_id = new_last_id

            if chunk_no % 10 == 0:
                logger.info(
                    "reclassify_and_rescore progress: chunk %d, %d checked, "
                    "%d reclassified, %d rescored so far",
                    chunk_no, total_checked, total_reclassified, total_rescored,
                )

        logger.info(
            "reclassify_and_rescore complete: %d chunks, %d jobs, "
            "%d reclassified, %d rescored",
            chunk_no, total_checked, total_reclassified, total_rescored,
        )
        return {
            "checked": total_checked,
            "reclassified": total_reclassified,
            "rescored": total_rescored,
            "chunks": chunk_no,
        }

    except Exception as e:
        logger.exception("reclassify_and_rescore failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.maintenance_task.backfill_job_descriptions")
def backfill_job_descriptions(batch_size: int = 500, max_jobs: int | None = None):
    """Populate JobDescription rows for historical jobs that never had one.

    Regression finding 101: the scan pipeline only started writing
    ``JobDescription`` rows in a recent round, so historical Job rows
    have ``raw_json`` populated but no companion description row. The
    resume scorer's online raw_json fallback (F97) covers this at
    scoring time, but every read path that joins JobDescription
    (``/jobs/{id}/description``, the JD-text-only ATS test harness)
    sees empty rows. This task walks the gap once: for every Job
    whose ``raw_json`` is non-null and whose ``id`` doesn't appear in
    ``job_descriptions``, parse the raw_json via the same
    ``extract_description`` helper the scan pipeline uses and merge
    a JobDescription row.

    Idempotent — re-runs are safe because the WHERE clause filters
    out anything that already has a row. Batched to keep a single
    transaction's write set bounded (``batch_size`` jobs per
    commit). ``max_jobs`` lets ops cap a one-shot run for a smoke
    test before unleashing the full sweep.

    Returns ``{"backfilled": int, "skipped_no_text": int, "scanned": int}``
    so admins can see what landed and what couldn't be backfilled
    (raw_json present but extractor returned empty — usually means
    the platform mapping in ``extract_description`` doesn't cover
    that row's shape, which is a separate finding).
    """
    logger.info("Starting backfill_job_descriptions (batch_size=%d max_jobs=%s)",
                batch_size, max_jobs)
    session = SyncSession()
    try:
        # IDs that already have a description — exclude them from the
        # scan. One IN-list lookup per batch is cheap; doing it inside
        # the per-job loop would N+1 across the whole table.
        existing_subq = select(JobDescription.job_id).subquery()

        scanned = 0
        backfilled = 0
        skipped_no_text = 0

        while True:
            if max_jobs is not None and scanned >= max_jobs:
                break
            # Limit to the batch size, ordered by oldest first so
            # repeated invocations make consistent forward progress.
            limit = batch_size
            if max_jobs is not None:
                limit = min(limit, max_jobs - scanned)
            jobs = session.execute(
                select(Job)
                .where(
                    Job.raw_json.isnot(None),
                    Job.id.notin_(select(existing_subq.c.job_id)),
                )
                .order_by(Job.first_seen_at.asc())
                .limit(limit)
            ).scalars().all()
            if not jobs:
                break

            now = datetime.now(timezone.utc)
            for job in jobs:
                scanned += 1
                html_content, text_content = extract_description(
                    job.platform or "", job.raw_json or {}
                )
                if not text_content and not html_content:
                    # Extractor couldn't parse this row — log and skip.
                    # The scoring fallback handles this case at scoring
                    # time, but persisting an empty row would mask the
                    # gap from monitoring (F101 explicitly wants a
                    # `has_description` signal).
                    skipped_no_text += 1
                    continue
                word_count = len(text_content.split()) if text_content else 0
                session.add(
                    JobDescription(
                        id=uuid.uuid4(),
                        job_id=job.id,
                        html_content=html_content,
                        text_content=text_content,
                        word_count=word_count,
                        fetched_at=now,
                    )
                )
                backfilled += 1

            session.commit()
            logger.info(
                "backfill_job_descriptions: batch done — scanned=%d backfilled=%d skipped=%d",
                scanned, backfilled, skipped_no_text,
            )

        logger.info(
            "backfill_job_descriptions complete: scanned=%d backfilled=%d skipped_no_text=%d",
            scanned, backfilled, skipped_no_text,
        )
        return {
            "scanned": scanned,
            "backfilled": backfilled,
            "skipped_no_text": skipped_no_text,
        }
    except Exception as e:
        logger.exception("backfill_job_descriptions failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.maintenance_task.auto_target_companies")
def auto_target_companies():
    """Mark companies as targets if they have 2+ relevant (clustered) active jobs.

    This ensures companies actively hiring for infra/security roles get higher
    scoring weight (company_fit_score: 1.0 vs 0.3).
    """
    logger.info("Starting auto_target_companies")
    session = SyncSession()

    try:
        # F313 (data correctness): filter to RELEVANT clusters only
        # rather than treating "any non-empty cluster" as relevant.
        # Pre-fix the WHERE clause was ``role_cluster != "" AND
        # role_cluster IS NOT NULL`` — counting any classified job.
        # Admins who add a cluster like ``data`` with
        # ``is_relevant=False`` (just for analytics tracking) had
        # those jobs flip companies to ``is_target=True`` against
        # intent. Now matches the same relevant-cluster set used
        # by the user-facing /jobs?role_cluster=relevant filter and
        # the /companies/scores leaderboard.
        from app.workers.tasks._role_matching import get_relevant_clusters_sync
        relevant_clusters = get_relevant_clusters_sync(session)

        # Find companies with 2+ relevant-cluster active jobs
        active_statuses = ["new", "under_review", "accepted"]
        result = session.execute(
            select(Job.company_id, func.count(Job.id))
            .where(
                Job.status.in_(active_statuses),
                Job.role_cluster.in_(relevant_clusters),
            )
            .group_by(Job.company_id)
            .having(func.count(Job.id) >= 2)
        )
        qualifying_ids = [row[0] for row in result]

        if not qualifying_ids:
            logger.info("auto_target_companies: no qualifying companies")
            return {"targeted": 0}

        # Mark these as targets (don't un-target existing manual targets)
        updated = session.execute(
            update(Company)
            .where(
                Company.id.in_(qualifying_ids),
                Company.is_target.is_(False),
            )
            .values(is_target=True)
        )
        count = updated.rowcount
        session.commit()

        logger.info("auto_target_companies: %d companies targeted (%d qualifying)",
                     count, len(qualifying_ids))
        return {"targeted": count, "qualifying": len(qualifying_ids)}

    except Exception as e:
        logger.exception("auto_target_companies failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.maintenance_task.fix_stuck_enrichments")
def fix_stuck_enrichments():
    """Reset companies stuck in 'enriching' status for more than 1 hour."""
    logger.info("Starting fix_stuck_enrichments")
    session = SyncSession()

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

        result = session.execute(
            update(Company)
            .where(
                Company.enrichment_status == "enriching",
                Company.updated_at < cutoff,
            )
            .values(
                enrichment_status="pending",
                enrichment_error="Reset from stuck enriching state",
            )
        )
        count = result.rowcount
        session.commit()

        logger.info("fix_stuck_enrichments: %d companies reset", count)
        return {"reset": count}

    except Exception as e:
        logger.exception("fix_stuck_enrichments failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.maintenance_task.fix_stuck_discovery_runs")
def fix_stuck_discovery_runs():
    """Sweep DiscoveryRun rows stuck in 'pending' or 'running' for >1 hour.

    Regression finding 186: before the API/worker wiring fix, any
    ``POST /discovery/runs`` call left a 'pending' row orphaned
    forever. Even post-fix we still need a safety net for cases
    where Celery was unreachable at dispatch time, or the worker
    crashed mid-run. Marks anything older than the cutoff as
    'failed' so admins can see failure in the UI and re-trigger,
    rather than staring at a spinner.
    """
    logger.info("Starting fix_stuck_discovery_runs")
    session = SyncSession()

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

        result = session.execute(
            update(DiscoveryRun)
            .where(
                DiscoveryRun.status.in_(["pending", "running"]),
                DiscoveryRun.started_at < cutoff,
            )
            .values(
                status="failed",
                completed_at=datetime.now(timezone.utc),
            )
        )
        count = result.rowcount
        session.commit()

        logger.info("fix_stuck_discovery_runs: %d runs reset to 'failed'", count)
        return {"reset": count}

    except Exception as e:
        logger.exception("fix_stuck_discovery_runs failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()
