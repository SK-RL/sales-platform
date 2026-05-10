"""ATS board scanning tasks."""

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession
from app.workers.tasks._scoring import compute_relevance_score
from app.workers.tasks._role_matching import match_role, match_role_with_config, classify_geography, load_cluster_config_sync
from app.workers.tasks._feedback import get_feedback_adjustment
from app.models.scoring_signal import ScoringSignal
from app.models.company import Company, CompanyATSBoard
from app.models.job import Job, JobDescription
from app.models.scan import ScanLog
from app.utils.company_name import looks_like_junk_company_name, looks_synthetic_company_name
from app.utils.job_description import extract_description
from app.utils.scan_lock import release_scan_lock

logger = logging.getLogger(__name__)

# Thread pool for running async fetchers from sync Celery context
_executor = ThreadPoolExecutor(max_workers=4)


def _load_signals_cache_sync(session: Session) -> dict:
    """F308 helper — load the global ``ScoringSignal`` table into a
    flat ``signal_key -> weight`` dict, mirroring the pattern in
    ``maintenance_task.rescore_jobs``.

    Used at scan time so newly-ingested jobs get the same
    feedback-adjusted score the next nightly rescore would compute.
    Without this, scan-time scores diverge from rescore-time scores
    in the window between scan and the next rescore — a freshly
    inserted job from a heavily-downvoted company would score high
    until the rescore caught it.

    Caller invokes this ONCE per scan-task entrypoint; the dict is
    threaded through ``_scan_board`` → ``_upsert_job``. Cheap query
    (signals table is small — one row per (cluster, geo, level,
    company) signal_key), so the per-scan load is fine.
    """
    rows = session.execute(select(ScoringSignal)).scalars().all()
    return {s.signal_key: s.weight for s in rows}


def _approved_roles_set_from_config(cluster_config: dict | None) -> set[str]:
    """F307 helper — build a flat lowercase ``approved_roles`` set
    from the admin's DB-stored ``RoleClusterConfig`` rows.

    Mirrors the construction in
    ``maintenance_task.rescore_jobs`` / ``reclassify_and_rescore``
    so scan-time scoring uses the SAME role superset that the
    rescore tasks use. Without this, a freshly-ingested job's
    score diverged from what the next nightly rescore would
    produce — admin-configured ``approved_roles`` were silently
    ignored at scan time.
    """
    if not cluster_config:
        return set()
    out: set[str] = set()
    for cfg in cluster_config.values():
        for role in (cfg.get("approved_roles") or []):
            if role:
                out.add(role.lower())
    return out

# Regression finding 7 (auto-deactivation): after this many *clean*
# zero-job scans in a row, flip the board's `is_active` to False.
# "Clean" = fetcher returned [] with no exception, so we know the slug
# is live and just empty. Fetcher errors (Cloudflare 403, network)
# never count toward or reset this streak — see `_update_board_health`.
#
# Threshold tuned for the daily scan cadence: 5 days of genuine empty
# responses is enough signal that the company has left the ATS, without
# deactivating boards that happen to be temporarily empty between
# posting cycles. Keep this constant (not a setting) — ops changes the
# behavior via manual `is_active` toggles, not by re-tuning the knob.
_STALE_BOARD_ZERO_SCAN_THRESHOLD = 5


def _upsert_job_description(session: Session, job_id: uuid.UUID, html_content: str, text_content: str) -> None:
    """Upsert a `JobDescription` row for the given `job_id`.

    Regression finding 97: the scan pipeline was persisting `Job` rows
    without ever writing the description text anywhere the ATS scorer
    could read it. Result: the keyword-extraction step received an empty
    string for 50%+ of rows, fell through to the role-cluster baseline,
    and every infra job produced the same 18 matched/6 missing keyword
    lists — so the scoring column collapsed to 4 distinct values.

    Called from `_upsert_job` after the parent `Job` has been added /
    updated in the session. For new jobs the `Job` row hasn't flushed
    yet, but the FK will bind at the outer commit because both writes
    live in the same session/transaction.

    No-op if neither ``html_content`` nor ``text_content`` is populated —
    we never clobber an existing populated row with empty strings, which
    would happen on platforms where the description lives only in the
    posting detail endpoint (not the list endpoint our fetchers hit).
    """
    if not html_content and not text_content:
        return

    existing = session.execute(
        select(JobDescription).where(JobDescription.job_id == job_id)
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    word_count = len((text_content or "").split())

    if existing:
        # Idempotent — skip the write if nothing meaningful changed. Keeps
        # `fetched_at` stable across scan cycles that re-hit the same
        # unchanged posting and avoids no-op WAL traffic.
        if (
            existing.text_content == text_content
            and existing.html_content == html_content
        ):
            return
        existing.html_content = html_content
        existing.text_content = text_content
        existing.word_count = word_count
        existing.fetched_at = now
    else:
        session.add(
            JobDescription(
                id=uuid.uuid4(),
                job_id=job_id,
                html_content=html_content,
                text_content=text_content,
                word_count=word_count,
                fetched_at=now,
            )
        )


def _update_board_health(board: CompanyATSBoard, stats: dict) -> None:
    """Advance the staleness counter / auto-deactivate if the streak hits.

    Called from `_scan_board` after a successful fetcher call (the
    fetcher-raised path never reaches us — it rollbacks via the outer
    except). Mutates `board` in place; the caller is responsible for
    committing. No return value — the helper is a pure state machine
    over the three cases below.

    Cases (in the order they're tested):
    1. `jobs_found >= 1`: board is alive. Reset the counter to 0 and,
       as defense-in-depth, clear any prior auto-deactivation reason
       in case an admin manually reactivated and the board has come
       back to life on its own.
    2. `errors > 0` with `jobs_found == 0`: cannot happen in practice
       today — per-job errors only fire inside the `for raw_job`
       loop, which only runs when `jobs_found >= 1`. But the guard
       is cheap and keeps the "errors don't count" invariant
       explicit for future callers.
    3. `jobs_found == 0` with `errors == 0`: clean zero return.
       Increment the counter and, if we've crossed the threshold
       while still active, flip `is_active=False` and stamp the
       reason. The `board.is_active` check makes this idempotent —
       we never re-deactivate an already-deactivated board and
       overwrite a human-set reason.
    """
    jobs_found = stats.get("jobs_found", 0)
    errors = stats.get("errors", 0)

    if jobs_found >= 1:
        board.consecutive_zero_scans = 0
        if board.deactivated_reason:
            board.deactivated_reason = ""
        return

    if errors > 0:
        # Defensive no-op — see docstring case 2.
        return

    # Clean-zero streak advances.
    board.consecutive_zero_scans += 1
    if (
        board.consecutive_zero_scans >= _STALE_BOARD_ZERO_SCAN_THRESHOLD
        and board.is_active
    ):
        board.is_active = False
        board.deactivated_reason = (
            f"auto: {board.consecutive_zero_scans} consecutive zero-job "
            f"scans (threshold {_STALE_BOARD_ZERO_SCAN_THRESHOLD})"
        )
        logger.info(
            "Auto-deactivated board %s/%s after %d consecutive zero-job scans",
            board.platform, board.slug, board.consecutive_zero_scans,
        )


def _trigger_alerts_for_new_jobs(session: Session):
    """Find new high-score jobs from the last scan and trigger alert notifications."""
    try:
        from app.models.alert import AlertConfig
        # Check if any alert configs exist
        has_alerts = session.execute(
            select(AlertConfig.id).where(AlertConfig.is_active.is_(True)).limit(1)
        ).scalar_one_or_none()
        if not has_alerts:
            return

        # Find new jobs from the last hour with score >= 50 (lowest possible threshold)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        new_jobs = session.execute(
            select(Job.id).where(
                Job.first_seen_at >= cutoff,
                Job.relevance_score >= 50,
            )
        ).scalars().all()

        if new_jobs:
            job_ids = [str(j) for j in new_jobs]
            from app.workers.tasks.alert_task import check_and_send_alerts
            check_and_send_alerts.delay(job_ids)
            logger.info("Triggered alerts for %d new high-score jobs", len(job_ids))
    except Exception as e:
        logger.warning("Alert trigger failed (non-fatal): %s", e)


def _get_fetcher_for_platform(platform: str):
    """Return an instantiated fetcher for a given ATS platform.

    Each fetcher has a .fetch(slug) method returning a list of normalized dicts.
    """
    from app.fetchers import FETCHER_MAP

    fetcher_cls = FETCHER_MAP.get(platform)
    if fetcher_cls is None:
        return None
    return fetcher_cls()


def _upsert_job(
    session: Session,
    company: Company,
    board: CompanyATSBoard,
    raw_job: dict,
    cluster_config: dict | None = None,
    approved_roles_set: set[str] | None = None,
    signals_cache: dict | None = None,
) -> str:
    """Upsert a single job record. Returns 'new', 'updated', or 'skipped'.

    F307 (data-reliability fix): pre-fix the scan-time scoring path
    called ``compute_relevance_score`` WITHOUT ``approved_roles_set``,
    so the scorer fell back to the hardcoded ``INFRA_ROLES +
    SECURITY_ROLES + QA_ROLES`` superset. Meanwhile
    ``rescore_jobs`` / ``reclassify_and_rescore`` build
    ``approved_roles_set`` from the admin's DB-stored
    ``RoleClusterConfig.approved_roles`` and pass it through.
    Net effect: a freshly-ingested job's score diverged from what
    the next nightly rescore would compute — admin-configured
    approved-role lists were silently ignored at scan time. F307
    threads ``approved_roles_set`` through ``_upsert_job`` and on
    into ``compute_relevance_score`` so scan-time scores match
    reclassify-time scores.
    """
    external_id = raw_job.get("external_id", "")
    if not external_id:
        return "skipped"

    title = raw_job.get("title", "").strip()

    # --- Validation: reject garbage data ---
    # Skip empty or too-short titles
    if not title or len(title) < 5:
        return "skipped"

    # Skip titles that are clearly not job postings
    _title_lower = title.lower()
    garbage_signals = [
        "test", "template", "do not apply", "internal only",
        "dummy", "example", "placeholder",
    ]
    if any(_title_lower == g for g in garbage_signals):
        return "skipped"

    # F243 (Khushi Jain, "Data Fetch", 2026-04-17): self-heal synthetic
    # company names from the ATS payload. Admin-added boards seed
    # Company.name from a free-text form field (see POST /api/v1/platforms
    # /boards), and submit-link flows use the slug verbatim. If an admin
    # typed "FormativeGroup" for slug "formativgroup", the row stayed
    # stuck on that spelling even after scans successfully pulled the
    # canonical "FormativGroup" from Greenhouse's v1 JSON — nothing ever
    # wrote the ATS-provided name back into Company.name.
    #
    # When the current Company.name looks synthetic (matches the slug
    # verbatim, matches `slug.replace("-", " ").title()`, or collapses
    # to the same lowercase-alphanumerics as the slug), prefer the raw
    # `company_name` from the ATS payload. Admin-curated names like
    # "Stripe, Inc." or "Alphabet" (for the google slug) do NOT collapse
    # to the slug's alphanumerics, so they're preserved.
    #
    # Runs per-job rather than per-board because Greenhouse is the only
    # fetcher that consistently populates `raw_json.company_name`, and
    # it does so on every job. Once the first job in a scan writes the
    # canonical name, `looks_synthetic_company_name` returns False for
    # subsequent jobs in the same session/transaction (the updated name
    # no longer looks synthetic), so the guard is self-limiting.
    raw_company_name = ((raw_job.get("raw_json") or {}).get("company_name") or "").strip()
    if (
        raw_company_name
        and raw_company_name != company.name
        and looks_synthetic_company_name(company.name, company.slug)
    ):
        logger.info(
            "F243 self-heal: company name %r -> %r (slug=%r, platform=%s, board_id=%s)",
            company.name, raw_company_name, company.slug, board.platform, board.id,
        )
        company.name = raw_company_name

    location_raw = raw_job.get("location_raw", "") or ""
    remote_scope = raw_job.get("remote_scope", "") or ""

    # Role matching
    role_match = match_role_with_config(title, cluster_config)
    matched_role = role_match["matched_role"]
    role_cluster = role_match["role_cluster"]
    title_normalized = role_match["title_normalized"]

    existing = session.execute(
        select(Job).where(Job.external_id == external_id)
    ).scalar_one_or_none()

    # Regression finding 88: aggregator boards (e.g. Jobgether on Lever)
    # re-post the same logical role with a new Lever job-id every few
    # hours, producing rows with distinct `external_id` but identical
    # `(company_id, title)`. The unique constraint on `external_id`
    # can't help here. Before inserting a brand-new row, look for an
    # existing Job that already covers this `(company_id, title)` — if
    # found, treat it as an update of that row (refresh `last_seen_at`,
    # re-score, etc.) and skip the insert entirely. This keeps the DB
    # at one row per logical role without requiring per-platform
    # "is_aggregator" annotations.
    #
    # F316 (data correctness) — three changes to the original F88
    # match:
    #   (a) Case + whitespace insensitive (``lower(trim(title))``)
    #       so ``"Senior SRE"`` and ``"senior sre  "`` from two
    #       ATS sources collapse correctly. Pre-fix exact-string
    #       equality let case/whitespace variants slip past.
    #   (b) Filtered to ACTIVE statuses so a previously-rejected
    #       role being re-listed today legitimately creates a new
    #       row (admin's reject decision wasn't ours to override).
    #   (c) Aligns with the partial UNIQUE INDEX
    #       ``uq_jobs_active_company_title`` shipped in migration
    #       ``m9n0o1p2q3r4`` — same predicate, so what the handler
    #       finds matches what the DB constraint enforces.
    if not existing and title:
        existing = session.execute(
            select(Job).where(
                Job.company_id == company.id,
                func.lower(func.trim(Job.title)) == title.strip().lower(),
                Job.status.in_(("new", "under_review", "accepted")),
            ).limit(1)
        ).scalar_one_or_none()

    # Cross-platform soft-match dedup (Tier-1 quality PR, 2026-04-17).
    # F88 above catches EXACT title matches (e.g. "Senior SRE" reposted
    # on Lever with a new external_id), but during ATS migrations a
    # company can list the SAME logical role with slightly different
    # wording: "Senior SRE" on Greenhouse vs "Sr. Site Reliability
    # Engineer" on Lever. Both normalize to the same cluster role via
    # `match_role_with_config`, so if we match on `title_normalized`
    # we collapse those into one Job row.
    #
    # Guards:
    #   * Only triggers when `title_normalized` is non-empty — otherwise
    #     EVERY unclassified job would collide into one row (role
    #     matcher returns `""` for titles that don't map to any
    #     cluster). That'd be catastrophic; the guard is load-bearing.
    #   * Skips jobs older than 90 days — a company re-posting a role
    #     they'd closed months ago is a legitimately new listing, not
    #     a dup. Limits the match to still-active-ish rows.
    #   * Only applies if neither `external_id` nor exact-title match
    #     found anything above — keeps this as the "last resort" match
    #     ahead of a fresh insert.
    # Track if this lookup was a cross-platform collapse, so the update
    # branch below can record the sighting in _also_seen_on AFTER it
    # reassigns `existing.raw_json = raw_job.get("raw_json", {})`. If
    # we wrote _also_seen_on here, the reassignment would clobber it.
    cross_platform_sighting: str | None = None
    carried_forward_also_seen: list[str] = []
    if not existing and title_normalized:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        existing = session.execute(
            select(Job).where(
                Job.company_id == company.id,
                Job.title_normalized == title_normalized,
                # Only collapse against still-active-ish rows. A closed
                # role being re-posted is a new listing.
                Job.status.in_(("new", "under_review", "accepted")),
                Job.first_seen_at >= cutoff,
            )
            .order_by(Job.first_seen_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing and existing.platform != board.platform:
            # Cross-platform hit. Capture the sighting key + preserve
            # any prior _also_seen_on entries from the existing row so
            # the update branch's raw_json reassignment doesn't drop
            # them. Actual write happens post-reassignment.
            cross_platform_sighting = f"{board.platform}:{external_id}"
            carried_forward_also_seen = list(
                (existing.raw_json or {}).get("_also_seen_on", [])
            )
            logger.info(
                "Cross-platform dedup: role %r already exists on %s (job_id=%s) — "
                "collapsing new listing from %s into existing row",
                title_normalized, existing.platform, existing.id, board.platform,
            )

    # Geography classification — both legacy bucket and new policy
    # are written. ``classify_remote_policy`` is the source of truth;
    # the legacy bucket is derived from (policy, countries) so the
    # two columns can never disagree.
    from app.workers.tasks._role_matching import classify_remote_policy
    from app.utils.remote_policy import legacy_bucket_for, normalise_countries
    remote_policy, remote_policy_countries = classify_remote_policy(
        location_raw, remote_scope
    )
    remote_policy_countries = normalise_countries(remote_policy_countries)
    geography_bucket = legacy_bucket_for(remote_policy, remote_policy_countries)

    now = datetime.now(timezone.utc)

    if existing:
        existing.title = title
        existing.title_normalized = title_normalized
        existing.url = raw_job.get("url", existing.url)
        existing.location_raw = location_raw
        existing.remote_scope = remote_scope
        existing.department = raw_job.get("department") or ""
        existing.employment_type = raw_job.get("employment_type") or ""
        existing.salary_range = raw_job.get("salary_range") or ""
        existing.matched_role = matched_role
        existing.role_cluster = role_cluster
        existing.geography_bucket = geography_bucket
        existing.remote_policy = remote_policy
        existing.remote_policy_countries = remote_policy_countries
        existing.last_seen_at = now
        existing.raw_json = raw_job.get("raw_json", {})

        # If we arrived here via the cross-platform soft-match dedup
        # branch above, record the new sighting inside the freshly
        # reassigned raw_json. Doing it here (not in the lookup block)
        # means the `raw_job.get("raw_json", {})` overwrite above
        # doesn't clobber the marker. `carried_forward_also_seen`
        # preserves prior sightings from the existing row so a role
        # that's been seen on 3+ platforms accumulates them over time.
        if cross_platform_sighting:
            also_seen = list(carried_forward_also_seen)
            if cross_platform_sighting not in also_seen:
                also_seen.append(cross_platform_sighting)
            raw_merged = dict(existing.raw_json or {})
            raw_merged["_also_seen_on"] = also_seen
            existing.raw_json = raw_merged

        # Recalculate relevance score
        # F307: pass approved_roles_set so the scorer sees the
        # admin's DB config rather than falling back to the
        # hardcoded role superset.
        # F308: also include feedback_adjustment so the scan-time
        # score matches what the next nightly rescore would compute.
        # ``existing`` already has company_id / role_cluster /
        # geography_bucket / title — all the signals
        # ``get_feedback_adjustment`` needs.
        feedback_adj_existing = (
            get_feedback_adjustment(existing, signals_cache)
            if signals_cache else 0.0
        )
        existing.relevance_score = compute_relevance_score(
            title=title,
            matched_role=matched_role,
            role_cluster=role_cluster,
            is_target=company.is_target,
            geography_bucket=geography_bucket,
            remote_scope=remote_scope,
            platform=board.platform,
            posted_at=existing.posted_at,
            approved_roles_set=approved_roles_set,
            feedback_adjustment=feedback_adj_existing,
        )
        job_id_for_desc = existing.id
        action = "updated"
    else:
        posted_at = raw_job.get("posted_at")
        if isinstance(posted_at, str):
            try:
                posted_at = datetime.fromisoformat(posted_at)
            except (ValueError, TypeError):
                posted_at = None

        job = Job(
            id=uuid.uuid4(),
            external_id=external_id,
            company_id=company.id,
            title=title,
            title_normalized=title_normalized,
            url=raw_job.get("url", ""),
            platform=board.platform,
            location_raw=location_raw,
            remote_scope=remote_scope,
            department=raw_job.get("department") or "",
            employment_type=raw_job.get("employment_type") or "",
            salary_range=raw_job.get("salary_range") or "",
            geography_bucket=geography_bucket,
            remote_policy=remote_policy,
            remote_policy_countries=remote_policy_countries,
            matched_role=matched_role,
            role_cluster=role_cluster,
            relevance_score=0.0,
            status="new",
            posted_at=posted_at,
            first_seen_at=now,
            last_seen_at=now,
            raw_json=raw_job.get("raw_json", {}),
        )
        # Compute relevance score
        # F307: pass approved_roles_set — see the existing-job
        # branch above for the rationale.
        # F308: pass feedback_adjustment so a fresh job from a
        # heavily-downvoted (cluster, company, geo, level)
        # combination starts with the right score, not an inflated
        # one that gets corrected only at the next rescore.
        feedback_adj_new = (
            get_feedback_adjustment(job, signals_cache)
            if signals_cache else 0.0
        )
        job.relevance_score = compute_relevance_score(
            title=title,
            matched_role=matched_role,
            role_cluster=role_cluster,
            is_target=company.is_target,
            geography_bucket=geography_bucket,
            remote_scope=remote_scope,
            platform=board.platform,
            posted_at=posted_at,
            approved_roles_set=approved_roles_set,
            feedback_adjustment=feedback_adj_new,
        )
        session.add(job)

        # F316 (data correctness, race-safe): the partial UNIQUE
        # ``uq_jobs_active_company_title`` (migration
        # ``m9n0o1p2q3r4``) enforces ONE active row per
        # ``(company_id, lower(trim(title)))``. The lookups above
        # (external_id, F88 case-insensitive, F88 cross-platform
        # soft-match) catch dups serially, but a concurrent scan
        # from a sibling worker can still race past them: both
        # workers' ``existing`` lookups miss, both reach the
        # ``session.add`` here, and one of the flushes fails on
        # the partial UNIQUE.
        #
        # Catch that IntegrityError, roll the SAVEPOINT, then
        # re-fetch the active row by the same key the constraint
        # enforces and treat it as the existing-row update path.
        # ``session.flush`` triggers the constraint check now
        # (rather than at outer commit) so we can recover before
        # any other side-effects (JobDescription upsert below)
        # depend on ``job.id`` of a row that won't survive commit.
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError as exc:
            constraint = getattr(getattr(exc, "orig", None), "diag", None)
            constraint_name = (
                getattr(constraint, "constraint_name", "") if constraint else ""
            )
            raw_msg = str(exc).lower()
            if (
                "uq_jobs_active_company_title" in (constraint_name or "")
                or "uq_jobs_active_company_title" in raw_msg
            ):
                # Re-find the active survivor that beat us to the
                # insert and update IT instead. Same lookup shape
                # the F88 fallback uses so we land on the canonical
                # row.
                session.expunge(job)
                survivor = session.execute(
                    select(Job).where(
                        Job.company_id == company.id,
                        func.lower(func.trim(Job.title)) == title.strip().lower(),
                        Job.status.in_(("new", "under_review", "accepted")),
                    ).limit(1)
                ).scalar_one_or_none()
                if survivor is None:
                    # Constraint fired but the row's gone — re-raise
                    # so the outer SAVEPOINT in ``scan_all_platforms``
                    # rolls this one job and the rest of the batch
                    # survives.
                    raise
                # Apply the same metadata refresh the existing-row
                # branch above does. Skip the relevance-score
                # recompute (the survivor's score was already
                # updated by whoever won the race; redundant work).
                #
                # F316b: preserve the survivor's existing
                # ``_also_seen_on`` cross-platform tracking before
                # the raw_json reassign clobbers it. Same defense
                # the F88 cross-platform soft-match branch uses on
                # line ~414. Also stamp the new sighting since this
                # IntegrityError path IS effectively a cross-
                # platform observation (whoever won the race
                # inserted with our intended platform/external_id;
                # we're now seeing the same role from a sibling
                # source).
                carried_also_seen = list(
                    (survivor.raw_json or {}).get("_also_seen_on", [])
                )
                survivor.last_seen_at = now
                survivor.url = raw_job.get("url", survivor.url)
                survivor.location_raw = location_raw
                survivor.remote_scope = remote_scope
                survivor.raw_json = raw_job.get("raw_json", {})
                # Re-attach the prior sighting list + record the
                # current platform:external_id sighting on top.
                if carried_also_seen or board.platform != survivor.platform:
                    new_seen = list(carried_also_seen)
                    sighting = f"{board.platform}:{external_id}"
                    if sighting not in new_seen and board.platform != survivor.platform:
                        new_seen.append(sighting)
                    raw_merged = dict(survivor.raw_json or {})
                    if new_seen:
                        raw_merged["_also_seen_on"] = new_seen
                    survivor.raw_json = raw_merged
                job_id_for_desc = survivor.id
                action = "updated"
            else:
                raise
        else:
            job_id_for_desc = job.id
            action = "new"

    # Regression finding 97: populate JobDescription from the upstream
    # raw_json payload. Before this hook the scan pipeline threw the
    # description text away (fetchers kept it only inside `Job.raw_json`)
    # and the ATS scoring path — which reads `JobDescription.text_content`
    # — saw empty strings for >50% of rows. Kept here (rather than inside
    # each fetcher) so per-platform field knowledge lives in one place
    # (`app.utils.job_description`) and the scan logic stays generic.
    try:
        html_content, text_content = extract_description(
            board.platform, raw_job.get("raw_json") or {}
        )
        _upsert_job_description(session, job_id_for_desc, html_content, text_content)
    except Exception as exc:
        # Description write failure must never abort a whole `_upsert_job`
        # — the relevance-scoring + role-classification work above is what
        # the scan is actually contracted to do. Log and move on; the
        # nightly rescore / next scan will try again.
        logger.warning(
            "JobDescription upsert failed for %s/%s external_id=%s: %s",
            board.platform, board.slug, external_id, exc,
        )

    return action


def _scan_board(
    session: Session,
    board: CompanyATSBoard,
    cluster_config: dict | None = None,
    approved_roles_set: set[str] | None = None,
    signals_cache: dict | None = None,
) -> dict:
    """Scan a single ATS board and return scan statistics.

    F307 (data-reliability fix): ``approved_roles_set`` flows from
    the caller (built once per scan from
    ``RoleClusterConfig.approved_roles`` in the DB) into every
    ``_upsert_job`` call so scan-time scores use the admin's
    configured role list — same source of truth that
    ``rescore_jobs`` uses.
    """
    stats = {"jobs_found": 0, "new_jobs": 0, "updated_jobs": 0, "skipped_jobs": 0, "errors": 0, "error_message": ""}

    fetcher = _get_fetcher_for_platform(board.platform)
    if not fetcher:
        stats["errors"] = 1
        stats["error_message"] = f"No fetcher for platform: {board.platform}"
        return stats

    company = session.execute(
        select(Company).where(Company.id == board.company_id)
    ).scalar_one_or_none()

    if not company:
        stats["errors"] = 1
        stats["error_message"] = f"Company not found: {board.company_id}"
        return stats

    try:
        raw_jobs = fetcher.fetch(board.slug)
        stats["jobs_found"] = len(raw_jobs)

        # F252 diagnostic plumbing: when a fetcher exposes
        # ``last_diagnostic`` (HackerNewsFetcher does today; future
        # aggregator fetchers can opt in by setting the same
        # attribute), persist its hint to ``ScanLog.error_message``
        # for any 0-job run. ``error_message`` doubles as a debug
        # channel for "why was this scan empty?" in the admin
        # Scan Logs UI without needing SSH access to container logs.
        # No-op for fetchers that don't set the attribute, and
        # never overwrites a real error_message set later in the
        # except block (we only populate when stats["error_message"]
        # is still empty AND raw_jobs is empty).
        diagnostic = getattr(fetcher, "last_diagnostic", None)
        if not raw_jobs and diagnostic and not stats.get("error_message"):
            import json as _json
            try:
                hint = _json.dumps(diagnostic, default=str)[:500]
            except Exception:
                hint = str(diagnostic)[:500]
            stats["error_message"] = f"diagnostic: {hint}"

        # Aggregator platforms fetch jobs from many companies — resolve per-job
        # "hackernews" and "yc_waas" also register as aggregators:
        # each uses a single synthetic board (slug=`__all__`) where
        # individual jobs belong to different hirers. See the
        # fetcher modules for wire-level details.
        _AGGREGATOR_PLATFORMS = {
            "himalayas", "weworkremotely", "remoteok", "remotive",
            "hackernews", "yc_waas",
            # F335 — Working Nomads RSS aggregator. Single board
            # with slug='__all__'; per-item company resolution
            # happens inside the fetcher (dc:creator first, then
            # description-body regex, then "unknown").
            "workingnomads",
        }
        is_aggregator = board.platform in _AGGREGATOR_PLATFORMS and board.slug == "__all__"

        for raw_job in raw_jobs:
            try:
                # For aggregator platforms, resolve the actual company from job data
                job_company = company
                if is_aggregator:
                    raw_json = raw_job.get("raw_json", {})
                    # Each aggregator uses different field names for the company
                    agg_company_name = (
                        raw_job.get("company_name")
                        or raw_json.get("companyName", "")
                        or raw_json.get("company_name", "")
                        or raw_json.get("company", "")
                        or ""
                    ).strip()
                    if agg_company_name:
                        # Regression finding 37: drop LinkedIn/aggregator-noise
                        # company names at ingest. `#hashtag` harvests, pure
                        # numerics, staffing-agency shells, and scratch names
                        # like "name"/"1name" all used to land in Company and
                        # then pollute /companies and the Pipeline board.
                        if looks_like_junk_company_name(agg_company_name):
                            logger.info(
                                "scan_task: skipping junk company name %r from %s/%s",
                                agg_company_name, board.platform, board.slug,
                            )
                            stats["skipped_jobs"] += 1
                            continue
                        import re
                        agg_slug = re.sub(r"[^a-z0-9-]", "", agg_company_name.lower().replace(" ", "-"))[:100]
                        # Look up by slug first (unique), then by name
                        existing_co = session.execute(
                            select(Company).where(Company.slug == agg_slug)
                        ).scalar_one_or_none()
                        if not existing_co:
                            existing_co = session.execute(
                                select(Company).where(Company.name == agg_company_name)
                            ).scalar_one_or_none()
                        if existing_co:
                            job_company = existing_co
                        else:
                            # Wrap the insert in a SAVEPOINT so an IntegrityError
                            # on a concurrent duplicate (same slug / same name)
                            # does NOT rollback the outer transaction and wipe
                            # out every job we've already upserted in this batch.
                            try:
                                with session.begin_nested():
                                    job_company = Company(
                                        id=uuid.uuid4(),
                                        name=agg_company_name,
                                        slug=agg_slug,
                                        is_target=False,
                                    )
                                    session.add(job_company)
                            except Exception:
                                # Re-lookup by slug, then by name (uniqueness
                                # can live on either column depending on history)
                                existing_co = session.execute(
                                    select(Company).where(Company.slug == agg_slug)
                                ).scalar_one_or_none()
                                if not existing_co:
                                    existing_co = session.execute(
                                        select(Company).where(Company.name == agg_company_name)
                                    ).scalar_one_or_none()
                                if existing_co:
                                    job_company = existing_co
                                else:
                                    raise

                # F253 regression fix: per-job SAVEPOINT so one failing
                # row can't poison the whole batch. Pre-fix, the HN scan
                # produced 298 jobs but the FIRST one overflowed
                # ``Job.title_normalized String(500)`` — SQLAlchemy
                # rolled the session back, every subsequent job's
                # autoflush hit ``PendingRollbackError``, and ALL 298
                # jobs were lost on a single overflow. Wrapping the
                # ``_upsert_job`` call in a nested transaction
                # (PostgreSQL SAVEPOINT) means a per-job DB error is
                # local: the savepoint rolls back, the outer
                # transaction continues, and the remaining 297 jobs
                # land normally. The except block below still records
                # the error for observability, but the rest of the
                # batch survives.
                with session.begin_nested():
                    result = _upsert_job(
                        session, job_company, board, raw_job, cluster_config,
                        approved_roles_set=approved_roles_set,
                        signals_cache=signals_cache,
                    )
                if result == "new":
                    stats["new_jobs"] += 1
                elif result == "updated":
                    stats["updated_jobs"] += 1
                elif result == "skipped":
                    stats["skipped_jobs"] += 1
            except Exception as e:
                logger.error("Error upserting job %s: %s", raw_job.get("external_id", "?"), e, exc_info=True)
                stats["errors"] += 1
                # F266 — propagate the per-job error message into
                # ``stats["error_message"]`` so the monitoring page
                # surfaces a non-empty hint. Pre-fix the bare
                # ``stats["errors"] += 1`` left the message field
                # empty, and the HN scanner reported errors=2 every
                # nightly run with no clue what failed (see /monitoring
                # /scan-errors — sample showed 3+ days of empty
                # err_excerpt for hackernews/__all__). Now we keep
                # the FIRST per-job exception's message + record how
                # many subsequent errors occurred. ``errs`` counter
                # is the existing ``stats["errors"]``; we just add a
                # text channel that's currently silent.
                if not stats.get("error_message"):
                    ext = (raw_job.get("external_id") or "?")[:30]
                    stats["error_message"] = f"job {ext}: {str(e)[:400]}"
                # else: subsequent errors keep stats["errors"]++ but
                # don't overwrite the first message — admin sees
                # "errors=N · first_error_msg" rather than a churn of
                # last-wins messages that hide the leading cause.

        # Update last_scanned_at + staleness health on the board.
        # `_update_board_health` mutates the board row in place (counter,
        # possibly is_active + deactivated_reason) — both writes share
        # this single commit so a crash between them can't leave the
        # counter updated without the deactivation flag (or vice versa).
        board.last_scanned_at = datetime.now(timezone.utc)
        _update_board_health(board, stats)
        session.commit()

    except Exception as e:
        logger.error("Error scanning board %s/%s: %s", board.platform, board.slug, e)
        stats["errors"] += 1
        stats["error_message"] = str(e)[:500]
        session.rollback()

    return stats


@celery_app.task(name="app.workers.tasks.scan_task.scan_all_platforms", bind=True, max_retries=2)
def scan_all_platforms(self):
    """Iterate all active CompanyATSBoard records and scan each one."""
    logger.info("Starting scan_all_platforms")
    start_time = time.time()

    session = SyncSession()
    try:
        cluster_config = load_cluster_config_sync(session)
        # F307: build approved_roles_set once per scan and thread
        # through to _scan_board → _upsert_job → compute_relevance_score
        approved_roles_set = _approved_roles_set_from_config(cluster_config)
        # F308: load signals cache once per scan so feedback signals
        # apply to fresh ingest without waiting for the next nightly
        # rescore. Cheap query (small table) — single round trip.
        signals_cache = _load_signals_cache_sync(session)

        boards = session.execute(
            select(CompanyATSBoard).where(CompanyATSBoard.is_active.is_(True))
        ).scalars().all()

        total_stats = {"jobs_found": 0, "new_jobs": 0, "updated_jobs": 0, "errors": 0}

        for board in boards:
            scan_log = ScanLog(
                id=uuid.uuid4(),
                source=f"{board.platform}/{board.slug}",
                platform=board.platform,
            )
            session.add(scan_log)
            session.flush()

            board_start = time.time()
            stats = _scan_board(
                session, board, cluster_config,
                approved_roles_set=approved_roles_set,
                signals_cache=signals_cache,
            )
            board_duration = int((time.time() - board_start) * 1000)

            scan_log.completed_at = datetime.now(timezone.utc)
            scan_log.jobs_found = stats["jobs_found"]
            scan_log.new_jobs = stats["new_jobs"]
            scan_log.updated_jobs = stats["updated_jobs"]
            scan_log.errors = stats["errors"]
            scan_log.error_message = stats["error_message"]
            scan_log.duration_ms = board_duration
            session.commit()

            for key in total_stats:
                total_stats[key] += stats.get(key, 0)

        total_duration = int((time.time() - start_time) * 1000)
        logger.info(
            "scan_all_platforms complete: %d boards, %d found, %d new, %d updated, %d errors, %dms",
            len(boards), total_stats["jobs_found"], total_stats["new_jobs"],
            total_stats["updated_jobs"], total_stats["errors"], total_duration,
        )

        # Trigger job alerts for new high-score jobs
        if total_stats["new_jobs"] > 0:
            _trigger_alerts_for_new_jobs(session)

        return total_stats

    except Exception as e:
        logger.exception("scan_all_platforms failed: %s", e)
        session.rollback()
        raise self.retry(exc=e, countdown=60)
    finally:
        session.close()
        # Finding 82: release the Redis concurrency lock acquired by
        # POST /platforms/scan/all. Runs on success, failure, and retry
        # so back-to-back scans are possible once this one finishes.
        release_scan_lock("all")


@celery_app.task(name="app.workers.tasks.scan_task.scan_platform", bind=True, max_retries=2)
def scan_platform(self, platform_name: str):
    """Scan all active boards for a specific platform."""
    logger.info("Starting scan_platform: %s", platform_name)
    start_time = time.time()

    session = SyncSession()
    try:
        cluster_config = load_cluster_config_sync(session)
        # F307: build approved_roles_set once per scan and thread
        # through to _scan_board → _upsert_job → compute_relevance_score
        approved_roles_set = _approved_roles_set_from_config(cluster_config)
        # F308: load signals cache once per scan so feedback signals
        # apply to fresh ingest without waiting for the next nightly
        # rescore. Cheap query (small table) — single round trip.
        signals_cache = _load_signals_cache_sync(session)

        boards = session.execute(
            select(CompanyATSBoard).where(
                CompanyATSBoard.platform == platform_name,
                CompanyATSBoard.is_active.is_(True),
            )
        ).scalars().all()

        if not boards:
            logger.warning("No active boards for platform %s", platform_name)
            return {"status": "no_boards", "platform": platform_name}

        total_stats = {"jobs_found": 0, "new_jobs": 0, "updated_jobs": 0, "errors": 0}

        for board in boards:
            scan_log = ScanLog(
                id=uuid.uuid4(),
                source=f"{board.platform}/{board.slug}",
                platform=board.platform,
            )
            session.add(scan_log)
            session.flush()

            board_start = time.time()
            stats = _scan_board(
                session, board, cluster_config,
                approved_roles_set=approved_roles_set,
                signals_cache=signals_cache,
            )
            board_duration = int((time.time() - board_start) * 1000)

            scan_log.completed_at = datetime.now(timezone.utc)
            scan_log.jobs_found = stats["jobs_found"]
            scan_log.new_jobs = stats["new_jobs"]
            scan_log.updated_jobs = stats["updated_jobs"]
            scan_log.errors = stats["errors"]
            scan_log.error_message = stats["error_message"]
            scan_log.duration_ms = board_duration
            session.commit()

            for key in total_stats:
                total_stats[key] += stats.get(key, 0)

        total_duration = int((time.time() - start_time) * 1000)
        total_stats["platform"] = platform_name
        total_stats["boards_scanned"] = len(boards)
        logger.info(
            "scan_platform %s complete: %d boards, %d found, %d new, %dms",
            platform_name, len(boards), total_stats["jobs_found"],
            total_stats["new_jobs"], total_duration,
        )
        return total_stats

    except Exception as e:
        logger.exception("scan_platform %s failed: %s", platform_name, e)
        session.rollback()
        raise self.retry(exc=e, countdown=60)
    finally:
        session.close()
        # Finding 82: release the per-platform lock. Scoped by name so
        # different platforms can scan in parallel.
        release_scan_lock(f"platform:{platform_name}")


@celery_app.task(name="app.workers.tasks.scan_task.scan_single_board", bind=True, max_retries=2)
def scan_single_board(self, board_id: str):
    """Scan a single ATS board by its ID."""
    logger.info("Starting scan_single_board: %s", board_id)
    start_time = time.time()

    session = SyncSession()
    try:
        cluster_config = load_cluster_config_sync(session)
        # F307: build approved_roles_set once per scan and thread
        # through to _scan_board → _upsert_job → compute_relevance_score
        approved_roles_set = _approved_roles_set_from_config(cluster_config)
        # F308: load signals cache once per scan so feedback signals
        # apply to fresh ingest without waiting for the next nightly
        # rescore. Cheap query (small table) — single round trip.
        signals_cache = _load_signals_cache_sync(session)

        board = session.execute(
            select(CompanyATSBoard).where(CompanyATSBoard.id == board_id)
        ).scalar_one_or_none()

        if not board:
            logger.warning("Board not found: %s", board_id)
            return {"status": "not_found", "board_id": board_id}

        scan_log = ScanLog(
            id=uuid.uuid4(),
            source=f"{board.platform}/{board.slug}",
            platform=board.platform,
        )
        session.add(scan_log)
        session.flush()

        stats = _scan_board(
            session, board, cluster_config,
            approved_roles_set=approved_roles_set,
            signals_cache=signals_cache,
        )
        duration = int((time.time() - start_time) * 1000)

        scan_log.completed_at = datetime.now(timezone.utc)
        scan_log.jobs_found = stats["jobs_found"]
        scan_log.new_jobs = stats["new_jobs"]
        scan_log.updated_jobs = stats["updated_jobs"]
        scan_log.errors = stats["errors"]
        scan_log.error_message = stats["error_message"]
        scan_log.duration_ms = duration
        session.commit()

        stats["board_id"] = board_id
        stats["platform"] = board.platform
        stats["slug"] = board.slug
        logger.info(
            "scan_single_board %s/%s complete: %d found, %d new, %dms",
            board.platform, board.slug, stats["jobs_found"], stats["new_jobs"], duration,
        )
        return stats

    except Exception as e:
        logger.exception("scan_single_board %s failed: %s", board_id, e)
        session.rollback()
        raise self.retry(exc=e, countdown=60)
    finally:
        session.close()
        # Finding 82: release the per-board lock. Short 5-min TTL means
        # even if this finally is bypassed (process kill), the lock
        # self-heals quickly.
        release_scan_lock(f"board:{board_id}")


@celery_app.task(name="app.workers.tasks.scan_task.scan_single_company", bind=True, max_retries=2)
def scan_single_company(self, company_id: str):
    """Scan all ATS boards for a single company."""
    logger.info("Starting scan_single_company: %s", company_id)
    start_time = time.time()

    session = SyncSession()
    try:
        cluster_config = load_cluster_config_sync(session)
        # F307: build approved_roles_set once per scan and thread
        # through to _scan_board → _upsert_job → compute_relevance_score
        approved_roles_set = _approved_roles_set_from_config(cluster_config)
        # F308: load signals cache once per scan so feedback signals
        # apply to fresh ingest without waiting for the next nightly
        # rescore. Cheap query (small table) — single round trip.
        signals_cache = _load_signals_cache_sync(session)

        boards = session.execute(
            select(CompanyATSBoard).where(
                CompanyATSBoard.company_id == company_id,
                CompanyATSBoard.is_active.is_(True),
            )
        ).scalars().all()

        if not boards:
            logger.warning("No active boards for company %s", company_id)
            return {"status": "no_boards"}

        total_stats = {"jobs_found": 0, "new_jobs": 0, "updated_jobs": 0, "errors": 0}

        for board in boards:
            scan_log = ScanLog(
                id=uuid.uuid4(),
                source=f"{board.platform}/{board.slug}",
                platform=board.platform,
            )
            session.add(scan_log)
            session.flush()

            board_start = time.time()
            stats = _scan_board(
                session, board, cluster_config,
                approved_roles_set=approved_roles_set,
                signals_cache=signals_cache,
            )
            board_duration = int((time.time() - board_start) * 1000)

            scan_log.completed_at = datetime.now(timezone.utc)
            scan_log.jobs_found = stats["jobs_found"]
            scan_log.new_jobs = stats["new_jobs"]
            scan_log.updated_jobs = stats["updated_jobs"]
            scan_log.errors = stats["errors"]
            scan_log.error_message = stats["error_message"]
            scan_log.duration_ms = board_duration
            session.commit()

            for key in total_stats:
                total_stats[key] += stats.get(key, 0)

        total_duration = int((time.time() - start_time) * 1000)
        logger.info(
            "scan_single_company %s complete: %d boards, %d found, %d new, %dms",
            company_id, len(boards), total_stats["jobs_found"],
            total_stats["new_jobs"], total_duration,
        )
        return total_stats

    except Exception as e:
        logger.exception("scan_single_company %s failed: %s", company_id, e)
        session.rollback()
        raise self.retry(exc=e, countdown=60)
    finally:
        session.close()
