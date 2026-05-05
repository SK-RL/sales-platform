"""Claude Routine Apply — orchestration endpoints.

This router is the control-plane for the MCP-Chrome routine that
automates job applications. The routine (a Claude-driven browser
session) polls these endpoints to know:

  * Which jobs to target next                  (top-to-apply)
  * Whether the operator has killed the run    (kill-switch)
  * How to record a run's progress             (runs CRUD)
  * How to de-fingerprint generated text       (humanize helper)

Everything here is *operator-scoped* — a query returns only data
owned by the caller. There are no admin-override endpoints; the
routine runs as the user, and the user sees only their own runs.

Pre-flight gating
-----------------
``POST /routine/runs`` is the single choke-point that decides
whether a run is allowed to start. It verifies:

  1. Kill-switch is OFF.
  2. Required answer-book coverage is 100% complete.
  3. Global daily cap (10 apps / 24h rolling) not hit.

The routine is expected to re-check these between applications too,
because a long run can race against a user toggling the kill-switch.
``GET /routine/top-to-apply`` returns the current state of all three
in its response envelope so a single poll is enough.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, func, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.deps import get_current_user
from app.models.answer_book import AnswerBookEntry
from app.models.application import Application
from app.models.application_submission import ApplicationSubmission
from app.models.company import Company
from app.models.humanization_corpus import HumanizationCorpus
from app.models.job import Job
from app.models.role_config import RoleClusterConfig
from app.models.resume import ResumeScore
from app.models.routine_kill_switch import RoutineKillSwitch
from app.models.routine_run import RoutineRun, ROUTINE_STATUSES
from app.models.routine_target import RoutineTarget, ROUTINE_TARGET_INTENTS
from app.models.user import User
from app.schemas.routine import (
    CreateRoutineRunRequest,
    CreateRoutineRunResponse,
    ExcludedCompany,
    HumanizeRequest,
    HumanizeResponse,
    KillSwitchRequest,
    KillSwitchResponse,
    RoutinePreferences,
    RoutineQueueResponse,
    RoutineRunDetail,
    RoutineRunOut,
    RoutineTargetCreate,
    RoutineTargetOut,
    SubmissionDetail,
    TopToApplyJob,
    TopToApplyResponse,
    UpdateRoutineRunRequest,
)
from app.services.answer_book_seed import REQUIRED_ENTRIES
from app.services.humanizer import humanize as humanize_text
from app.utils.audit import log_action

router = APIRouter(prefix="/routine", tags=["routine"])


# ═══════════════════════════════════════════════════════════════════
# Tunables — kept at module scope so tests can monkeypatch and the
# operator can see the constants without grep-diving. Values agreed
# during v6 planning.
# ═══════════════════════════════════════════════════════════════════

# Global cap on submitted applications per rolling 24h. The routine
# refuses to submit above this; top-to-apply reports the remaining.
DAILY_CAP = 10

# Per-company cooldown: once a user applies to a job at a company,
# we suppress every other job from the same company from top-to-apply
# for this many days. Prevents shotgun-blasting a single employer.
COMPANY_COOLDOWN_DAYS = 30

# Platforms excluded outright from the routine. LinkedIn has its own
# Easy Apply pipeline and anti-bot measures that make MCP-Chrome
# automation a detection nightmare; ship without it for v6.
EXCLUDED_PLATFORMS: frozenset[str] = frozenset({"linkedin"})

# Geography buckets the routine supports. "" (unclassified) is
# excluded because the salary-minimum lookup needs a bucket to pick.
ROUTINE_GEOGRAPHY_BUCKETS: tuple[str, ...] = ("global_remote", "usa_only", "uae_only")

# Minimum humanization_corpus rows required before style-match
# kicks in. Mirrored from the humanizer module so the router and
# the helper stay in sync.
STYLE_MATCH_MIN_CORPUS_SIZE = 10


# ═══════════════════════════════════════════════════════════════════
# Helpers — shared between top-to-apply and create-run
# ═══════════════════════════════════════════════════════════════════


async def _get_relevant_clusters(db: AsyncSession) -> list[str]:
    """Duplicate of the helper in companies.py / jobs.py.

    Intentionally copy-pasted rather than imported to keep this router
    decoupled from other v1 modules — if companies.py moves or the
    helper signature changes, the routine still works.
    """
    result = await db.execute(
        select(RoleClusterConfig.name).where(
            RoleClusterConfig.is_relevant == True,  # noqa: E712
            RoleClusterConfig.is_active == True,  # noqa: E712
        )
    )
    clusters = result.scalars().all()
    return list(clusters) if clusters else ["infra", "security"]


async def _count_recent_submissions(db: AsyncSession, user_id: UUID, hours: int = 24) -> int:
    """Count applications the user submitted in the last ``hours`` window.

    Daily-cap gating is defined on *live submissions* — a submission is
    "live" when the routine's confirm-submitted handler actually sent
    it to the ATS (vs. dry_run / single_trial). There are two places
    this signal can live:

      1. ``Application.status == "applied"`` with ``applied_at >= cutoff``
         — the nominal, post-commit state. Always counted.

      2. ``ApplicationSubmission`` rows from a live ``RoutineRun`` with
         ``submitted_at >= cutoff`` — catches the race window where the
         submission row was written but the Application status flip is
         still in-flight (network blip between handler steps, or a
         partial rollback that left the submission but not the status).

    We UNION both via a Python set on application_id so an application
    counted in both buckets isn't double-counted. Dry-run and single-
    trial submissions are excluded via the ``routine_runs.mode == 'live'``
    filter — those runs' submissions carry ``detected_issues=["dry_run"]``
    and shouldn't consume the cap.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Bucket 1: committed applies.
    committed_ids = (await db.execute(
        select(Application.id).where(
            Application.user_id == user_id,
            Application.status == "applied",
            Application.applied_at >= cutoff,
        )
    )).scalars().all()

    # Bucket 2: in-flight live-run submissions (may overlap bucket 1).
    in_flight_ids = (await db.execute(
        select(ApplicationSubmission.application_id)
        .join(Application, Application.id == ApplicationSubmission.application_id)
        .join(RoutineRun, RoutineRun.id == ApplicationSubmission.routine_run_id)
        .where(
            Application.user_id == user_id,
            RoutineRun.mode == "live",
            ApplicationSubmission.submitted_at >= cutoff,
        )
    )).scalars().all()

    return len(set(committed_ids) | set(in_flight_ids))


async def _required_coverage_complete(db: AsyncSession, user_id: UUID) -> bool:
    """Return True iff every required answer-book entry is filled.

    Pre-flight gate for both top-to-apply (advisory flag) and
    create-run (hard rejection). Keyed on ``is_locked=True`` +
    non-empty answer; cheaper than re-validating the full seed list
    on every call because the lock flag is the server-side truth.
    """
    required_keys = [qkey for (_cat, qkey, _q) in REQUIRED_ENTRIES]
    if not required_keys:
        return True

    # Count filled rows among the required set. A row is "filled"
    # when the answer trims to non-empty — same rule as
    # RequiredCoverageResponse.
    result = await db.execute(
        select(func.count(AnswerBookEntry.id)).where(
            AnswerBookEntry.user_id == user_id,
            AnswerBookEntry.is_locked == True,  # noqa: E712
            AnswerBookEntry.source == "manual_required",
            AnswerBookEntry.question_key.in_(required_keys),
            func.length(func.trim(AnswerBookEntry.answer)) > 0,
        )
    )
    filled = int(result.scalar() or 0)
    return filled == len(required_keys)


async def _kill_switch_disabled(db: AsyncSession, user_id: UUID) -> bool:
    """Return True when the user has actively disabled the routine."""
    row = (await db.execute(
        select(RoutineKillSwitch).where(RoutineKillSwitch.user_id == user_id)
    )).scalar_one_or_none()
    return bool(row and row.disabled)


# ═══════════════════════════════════════════════════════════════════
# GET /routine/top-to-apply
# ═══════════════════════════════════════════════════════════════════


def _load_user_preferences(user: User) -> RoutinePreferences:
    """Parse ``users.routine_preferences`` JSONB into a typed object.

    A fresh user has ``{}`` from the migration default → returns
    ``RoutinePreferences()`` with all-empty defaults (legacy behaviour).
    Anything unparseable (someone hand-mutated the column with junk)
    is logged and treated as empty so a bad row never wedges
    top-to-apply.
    """
    raw = getattr(user, "routine_preferences", None) or {}
    if not isinstance(raw, dict):
        return RoutinePreferences()
    try:
        return RoutinePreferences.model_validate(raw)
    except Exception:
        # Don't 500 the picker for one user with a bad JSONB value.
        return RoutinePreferences()


@router.get("/top-to-apply", response_model=TopToApplyResponse)
async def top_to_apply(
    limit: int = Query(10, ge=1, le=25),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pick the next N jobs for the routine to target.

    F257 changes — the operator now has real control via
    ``users.routine_preferences`` JSONB and the per-job
    ``routine_targets`` queue/exclude list:

      * ``intent='queued'`` rows are surfaced FIRST, above the
        relevance-picker rows. Pinned jobs render with ``is_queued=true``
        in the response so the UI can badge them.
      * ``intent='excluded'`` rows are filtered out entirely.
      * ``preferences.only_global_remote`` / ``allowed_geographies`` /
        ``allowed_role_clusters`` / ``min_relevance_score`` /
        ``min_resume_score`` / ``extra_excluded_platforms`` all narrow
        the auto-picker.

    Default filters still applied (always-on hard limits):
      * ``Job.platform NOT IN linkedin``  (Easy-Apply detection)
      * ``Job.status NOT IN ('expired', 'archived')``
      * No existing ``Application`` for (user, job)
      * No application to the same ``company_id`` in the last 30 days
      * Ordered by ``relevance_score`` DESC, then ``first_seen_at`` DESC

    Response envelope still carries the three pre-flight flags —
    ``kill_switch_active``, ``answer_book_ready``, ``daily_cap_remaining``
    — so the routine can decide "should I even start?" from a single
    poll.
    """
    prefs = _load_user_preferences(user)
    relevant_clusters = await _get_relevant_clusters(db)

    # Resolve the EFFECTIVE filter set for this picker call —
    # preferences narrow each list, but never widen past the
    # platform-wide allow-list.
    if prefs.allowed_role_clusters:
        # User-supplied clusters intersected with the platform's
        # currently-relevant set so a user can't sneak a cluster
        # the platform doesn't classify.
        effective_clusters = [c for c in prefs.allowed_role_clusters if c in relevant_clusters]
        if not effective_clusters:
            # User picked clusters that don't intersect — fall back to
            # the platform default so they don't end up with an empty
            # picker silently.
            effective_clusters = relevant_clusters
    else:
        effective_clusters = relevant_clusters

    if prefs.only_global_remote:
        effective_geographies = ("global_remote",)
    elif prefs.allowed_geographies:
        effective_geographies = tuple(prefs.allowed_geographies)
    else:
        effective_geographies = ROUTINE_GEOGRAPHY_BUCKETS

    excluded_platforms = set(EXCLUDED_PLATFORMS) | {
        p.lower().strip() for p in prefs.extra_excluded_platforms if p
    }

    # F259: per-user company-level exclude list. A no-fly list of
    # company UUIDs the picker drops outright. Coerced via ``set`` so a
    # caller passing duplicates from a buggy frontend doesn't bloat the
    # NOT IN clause.
    excluded_company_ids = {cid for cid in (prefs.excluded_company_ids or []) if cid is not None}

    # Jobs already in Application (any status) are skipped.
    applied_jobs_sub = select(Application.job_id).where(
        Application.user_id == user.id
    ).subquery()

    # Companies the user has applied to within the cooldown window.
    cooldown_cutoff = datetime.now(timezone.utc) - timedelta(days=COMPANY_COOLDOWN_DAYS)
    cooldown_companies_sub = (
        select(Job.company_id)
        .join(Application, Application.job_id == Job.id)
        .where(
            Application.user_id == user.id,
            Application.applied_at >= cooldown_cutoff,
            Job.company_id.is_not(None),
        )
    ).subquery()

    # F257: routine_targets — load both intents in one round-trip.
    targets_rows = (await db.execute(
        select(RoutineTarget.job_id, RoutineTarget.intent).where(
            RoutineTarget.user_id == user.id
        )
    )).all()
    queued_job_ids = {jid for jid, intent in targets_rows if intent == "queued"}
    excluded_job_ids = {jid for jid, intent in targets_rows if intent == "excluded"}

    # Optional resume-score floor — JOIN against the user's active
    # resume's ResumeScore row only if the user has an active resume
    # AND a non-zero floor is configured (the JOIN cost isn't worth
    # it otherwise).
    resume_score_join_active = bool(prefs.min_resume_score) and bool(user.active_resume_id)

    base_filters = [
        Job.platform.notin_(excluded_platforms),
        Job.role_cluster.in_(effective_clusters),
        Job.geography_bucket.in_(effective_geographies),
        Job.status.notin_(("expired", "archived")),
        Job.id.notin_(select(applied_jobs_sub.c.job_id)),
        and_(
            Job.company_id.is_not(None),
            Job.company_id.notin_(select(cooldown_companies_sub.c.company_id)),
        ),
    ]
    # F259: company-level exclude (per-user no-fly list). ``notin_([])``
    # is a no-op in SQL, so guard the empty-list case explicitly.
    if excluded_company_ids:
        base_filters.append(Job.company_id.notin_(excluded_company_ids))
    if prefs.min_relevance_score:
        base_filters.append(Job.relevance_score >= prefs.min_relevance_score)

    # ── Phase A: queued (operator-pinned) jobs ─────────────────────
    queued_jobs: list[tuple[Job, str | None]] = []
    if queued_job_ids:
        # Pinned jobs bypass the cluster / geography / score / company-
        # cooldown filters — the operator explicitly asked for them.
        # We DO still skip already-applied jobs and excluded platforms,
        # because those are hard safety rails (no double-apply, no
        # LinkedIn Easy-Apply). Pinned-but-excluded is impossible by
        # design (excluded set is queried and removed below).
        queued_q = (
            select(Job, Company.name.label("company_name"))
            .join(Company, Company.id == Job.company_id, isouter=True)
            .where(
                Job.id.in_(queued_job_ids - excluded_job_ids),
                Job.platform.notin_(excluded_platforms),
                Job.status.notin_(("expired", "archived")),
                Job.id.notin_(select(applied_jobs_sub.c.job_id)),
            )
            .order_by(Job.relevance_score.desc(), Job.first_seen_at.desc())
            .limit(limit)
        )
        queued_jobs = (await db.execute(queued_q)).all()

    # ── Phase B: relevance-picker jobs (filling out to ``limit``) ──
    auto_limit = max(0, limit - len(queued_jobs))
    auto_jobs: list[tuple[Job, str | None]] = []
    if auto_limit > 0:
        already_picked_ids = {j.id for j, _ in queued_jobs}
        auto_q = (
            select(Job, Company.name.label("company_name"))
            .join(Company, Company.id == Job.company_id, isouter=True)
            .where(
                *base_filters,
                Job.id.notin_(excluded_job_ids | already_picked_ids),
            )
            .order_by(Job.relevance_score.desc(), Job.first_seen_at.desc())
            .limit(auto_limit)
        )
        # Optional resume-score floor — apply post-query because the
        # ResumeScore JOIN would inflate the row count and the active-
        # resume scoring backlog is incomplete enough that an inner
        # join could silently empty the picker. Cheap to filter in
        # Python on a list bounded by ``limit`` (max 25 rows).
        if resume_score_join_active:
            score_floor = prefs.min_resume_score
            ranked_ids = [r.id for r, _ in (await db.execute(auto_q)).all()]
            scored = (await db.execute(
                select(ResumeScore.job_id, ResumeScore.overall_score).where(
                    ResumeScore.resume_id == user.active_resume_id,
                    ResumeScore.job_id.in_(ranked_ids),
                )
            )).all()
            ok_ids = {jid for jid, score in scored if (score or 0) >= score_floor}
            # Re-fetch with the ok_ids filter so we keep the JOIN-free
            # response shape.
            if ok_ids:
                auto_jobs = (await db.execute(
                    select(Job, Company.name.label("company_name"))
                    .join(Company, Company.id == Job.company_id, isouter=True)
                    .where(Job.id.in_(ok_ids))
                    .order_by(Job.relevance_score.desc(), Job.first_seen_at.desc())
                )).all()
        else:
            auto_jobs = (await db.execute(auto_q)).all()

    # Flags for the response envelope.
    kill_active = await _kill_switch_disabled(db, user.id)
    coverage_ready = await _required_coverage_complete(db, user.id)
    used_today = await _count_recent_submissions(db, user.id, hours=24)
    remaining = max(0, DAILY_CAP - used_today)

    def _to_out(job: Job, company_name: str | None, *, queued: bool) -> TopToApplyJob:
        return TopToApplyJob(
            job_id=job.id,
            title=job.title,
            company_id=job.company_id,
            company_name=company_name or "",
            platform=job.platform,
            relevance_score=float(job.relevance_score or 0.0),
            geography_bucket=job.geography_bucket or None,
            role_cluster=job.role_cluster or None,
            is_queued=queued,
        )

    jobs_out: list[TopToApplyJob] = []
    jobs_out.extend(_to_out(j, c, queued=True) for j, c in queued_jobs)
    jobs_out.extend(_to_out(j, c, queued=False) for j, c in auto_jobs)

    return TopToApplyResponse(
        kill_switch_active=kill_active,
        daily_cap_remaining=remaining,
        answer_book_ready=coverage_ready,
        jobs=jobs_out,
    )


# ═══════════════════════════════════════════════════════════════════
# F257 — Routine preferences + manual queue
# ═══════════════════════════════════════════════════════════════════


@router.get("/preferences", response_model=RoutinePreferences)
async def get_routine_preferences(
    user: User = Depends(get_current_user),
):
    """Return this user's ``RoutinePreferences``.

    Defaults to all-empty when the column is ``{}`` (fresh user).
    """
    return _load_user_preferences(user)


@router.put("/preferences", response_model=RoutinePreferences)
async def put_routine_preferences(
    body: RoutinePreferences,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace this user's ``routine_preferences`` JSONB.

    PUT semantics — the body fully replaces the stored prefs. Use the
    Pydantic defaults to "reset" a field to its zero value.
    """
    user.routine_preferences = body.model_dump(mode="json")
    await db.commit()
    await db.refresh(user)
    return _load_user_preferences(user)


def _hydrate_target(target: RoutineTarget, job: Job, company_name: str | None) -> RoutineTargetOut:
    """Build the wire-shape RoutineTargetOut from ORM rows.

    Pulled out so both the list endpoint and the single-job upsert
    endpoint return the same enriched shape without code duplication.
    """
    return RoutineTargetOut(
        id=target.id,
        job_id=target.job_id,
        intent=target.intent,
        note=target.note or "",
        created_at=target.created_at,
        updated_at=target.updated_at,
        job_title=job.title or "",
        company_name=company_name or "",
        job_url=job.url or "",
        relevance_score=float(job.relevance_score or 0.0),
        platform=job.platform or "",
    )


@router.get("/queue", response_model=RoutineQueueResponse)
async def get_routine_queue(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List this user's manually-queued + manually-excluded jobs.

    Both lists hydrate the job + company display fields so the UI
    can render the cards without N+1 round-trips per row.
    """
    rows = (await db.execute(
        select(RoutineTarget, Job, Company.name.label("company_name"))
        .join(Job, Job.id == RoutineTarget.job_id)
        .join(Company, Company.id == Job.company_id, isouter=True)
        .where(RoutineTarget.user_id == user.id)
        .order_by(RoutineTarget.updated_at.desc())
    )).all()

    queued: list[RoutineTargetOut] = []
    excluded: list[RoutineTargetOut] = []
    for target, job, company_name in rows:
        out = _hydrate_target(target, job, company_name)
        if target.intent == "queued":
            queued.append(out)
        elif target.intent == "excluded":
            excluded.append(out)

    return RoutineQueueResponse(queued=queued, excluded=excluded)


@router.post("/queue/{job_id}", response_model=RoutineTargetOut)
async def upsert_routine_target(
    job_id: UUID,
    body: RoutineTargetCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pin a job to the routine queue (``intent='queued'``) or block
    it from auto-picks (``intent='excluded'``).

    Re-posting against the same ``job_id`` updates the existing row
    in place (UNIQUE(user_id, job_id) enforces this) — switching
    intent or updating the note is a single PATCH-equivalent.
    """
    # Verify the job actually exists. Don't 404 on archived jobs —
    # the operator may want to re-include something the auto-cull
    # disabled. The picker's hard ``status NOT IN`` filter still
    # protects against archived/expired jobs sneaking into runs.
    job = (await db.execute(
        select(Job).where(Job.id == job_id)
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    company_name = None
    if job.company_id is not None:
        company_name = (await db.execute(
            select(Company.name).where(Company.id == job.company_id)
        )).scalar()

    existing = (await db.execute(
        select(RoutineTarget).where(
            RoutineTarget.user_id == user.id,
            RoutineTarget.job_id == job_id,
        )
    )).scalar_one_or_none()

    if existing is not None:
        existing.intent = body.intent
        existing.note = body.note
        existing.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(existing)
        return _hydrate_target(existing, job, company_name)

    # F323 (race-safe): the lookup-then-INSERT path is TOCTOU-
    # vulnerable to concurrent POSTs (two tabs queuing the same
    # job, double-click). Both pass the SELECT, both add a
    # RoutineTarget(user_id=X, job_id=Y), second hits
    # ``uq_routine_targets_user_job`` and 500s. Wrap in a
    # SAVEPOINT and on race-loss re-fetch the winning row, then
    # apply the same idempotent refresh path the existing-row
    # branch above does (intent/note from this request wins —
    # most-recent-write semantics).
    new_target = RoutineTarget(
        id=uuid.uuid4(),
        user_id=user.id,
        job_id=job_id,
        intent=body.intent,
        note=body.note,
    )
    db.add(new_target)
    from sqlalchemy.exc import IntegrityError
    try:
        async with db.begin_nested():
            await db.flush()
        existing = new_target
    except IntegrityError:
        db.expunge(new_target)
        existing = (await db.execute(
            select(RoutineTarget).where(
                RoutineTarget.user_id == user.id,
                RoutineTarget.job_id == job_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            raise
        existing.intent = body.intent
        existing.note = body.note
        existing.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(existing)
    return _hydrate_target(existing, job, company_name)


@router.delete("/queue/{job_id}", status_code=204)
async def delete_routine_target(
    job_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a job from the manual queue / exclude list entirely.

    No-op (still 204) if the row doesn't exist — idempotent so the
    UI can fire a delete without first checking whether the row is
    there.
    """
    target = (await db.execute(
        select(RoutineTarget).where(
            RoutineTarget.user_id == user.id,
            RoutineTarget.job_id == job_id,
        )
    )).scalar_one_or_none()
    if target is not None:
        await db.delete(target)
        await db.commit()
    return None


# ── F259: company-level exclude list ────────────────────────────────


def _save_preferences(user: User, prefs: RoutinePreferences) -> None:
    """Mutate ``users.routine_preferences`` in-place from a
    ``RoutinePreferences`` object. Caller commits the session."""
    user.routine_preferences = prefs.model_dump(mode="json")


@router.get("/excluded-companies", response_model=list[ExcludedCompany])
async def list_excluded_companies(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hydrate the user's ``excluded_company_ids`` list with company
    names + slugs so the UI doesn't need a second per-id round-trip
    to render the exclude card.

    Stale-id resilience: if the user's prefs reference a company id
    that's been deleted from the ``companies`` table, the row is
    simply omitted from the response (the picker side already
    tolerates stale ids — ``Job.company_id NOT IN (stale_id)`` is
    a no-op when no live job has that company_id).
    """
    prefs = _load_user_preferences(user)
    ids = list({cid for cid in (prefs.excluded_company_ids or []) if cid is not None})
    if not ids:
        return []
    rows = (await db.execute(
        select(Company.id, Company.name, Company.slug).where(Company.id.in_(ids))
    )).all()
    return [
        ExcludedCompany(id=cid, name=name or "", slug=slug or "")
        for cid, name, slug in rows
    ]


@router.post(
    "/excluded-companies/{company_id}",
    response_model=list[ExcludedCompany],
)
async def add_excluded_company(
    company_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a company to the user's no-fly list. Idempotent — adding
    a company that's already excluded is a no-op (still returns 200).

    Verifies the company exists so a typoed UUID 404s instead of
    silently bloating the JSONB list with dead ids.
    """
    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")

    prefs = _load_user_preferences(user)
    ids = set(prefs.excluded_company_ids or [])
    if company_id not in ids:
        ids.add(company_id)
        prefs = prefs.model_copy(update={"excluded_company_ids": sorted(ids, key=str)})
        _save_preferences(user, prefs)
        await db.commit()
        await db.refresh(user)

    # Return the full hydrated list so the caller can update its
    # local cache in one round-trip.
    return await list_excluded_companies(user=user, db=db)


@router.delete(
    "/excluded-companies/{company_id}",
    response_model=list[ExcludedCompany],
)
async def remove_excluded_company(
    company_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Drop a company from the user's no-fly list. Idempotent — a
    company that isn't on the list is a no-op."""
    prefs = _load_user_preferences(user)
    ids = set(prefs.excluded_company_ids or [])
    if company_id in ids:
        ids.discard(company_id)
        prefs = prefs.model_copy(update={"excluded_company_ids": sorted(ids, key=str)})
        _save_preferences(user, prefs)
        await db.commit()
        await db.refresh(user)
    return await list_excluded_companies(user=user, db=db)


# ═══════════════════════════════════════════════════════════════════
# Kill-switch — GET / POST
# ═══════════════════════════════════════════════════════════════════


@router.get("/kill-switch", response_model=KillSwitchResponse)
async def get_kill_switch(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Current kill-switch state for the caller.

    Absence of a row == ``disabled=False`` (the default). We don't
    auto-create rows on read to keep the table small — only flips
    to ``disabled=True`` write.
    """
    row = (await db.execute(
        select(RoutineKillSwitch).where(RoutineKillSwitch.user_id == user.id)
    )).scalar_one_or_none()
    if row is None:
        return KillSwitchResponse(disabled=False, disabled_at=None, reason=None)
    return KillSwitchResponse(
        disabled=row.disabled,
        disabled_at=row.disabled_at,
        reason=row.reason,
    )


@router.post("/kill-switch", response_model=KillSwitchResponse)
async def set_kill_switch(
    body: KillSwitchRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle the kill-switch.

    Writes an audit row on every flip (enabled→disabled and back) so
    an operator can reconstruct "why did the routine stop" from the
    audit log alone. ``reason`` is captured in both the DB row (so
    it survives a toggle-back) and the audit metadata.
    """
    row = (await db.execute(
        select(RoutineKillSwitch).where(RoutineKillSwitch.user_id == user.id)
    )).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if row is None:
        row = RoutineKillSwitch(
            user_id=user.id,
            disabled=body.disabled,
            disabled_at=now if body.disabled else None,
            reason=body.reason,
        )
        db.add(row)
    else:
        row.disabled = body.disabled
        row.disabled_at = now if body.disabled else None
        row.reason = body.reason

    await db.commit()
    await db.refresh(row)

    await log_action(
        db, user,
        action=("routine.kill_switch_on" if body.disabled else "routine.kill_switch_off"),
        resource="routine_kill_switch",
        request=request,
        metadata={"reason": body.reason},
    )

    return KillSwitchResponse(
        disabled=row.disabled,
        disabled_at=row.disabled_at,
        reason=row.reason,
    )


# ═══════════════════════════════════════════════════════════════════
# Routine runs — CRUD
# ═══════════════════════════════════════════════════════════════════


def _run_to_out(run: RoutineRun) -> RoutineRunOut:
    return RoutineRunOut(
        id=run.id,
        user_id=run.user_id,
        started_at=run.started_at,
        ended_at=run.ended_at,
        mode=run.mode,  # type: ignore[arg-type]
        applications_attempted=run.applications_attempted,
        applications_submitted=run.applications_submitted,
        applications_skipped=run.applications_skipped or [],
        detection_incidents=run.detection_incidents or [],
        status=run.status,  # type: ignore[arg-type]
        kill_switch_triggered=run.kill_switch_triggered,
    )


@router.post("/runs", response_model=CreateRoutineRunResponse)
async def create_run(
    body: CreateRoutineRunRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start a new routine run.

    Hard-rejects with 400 when any pre-flight fails. The routine is
    expected to fix the underlying problem (fill the answer book,
    flip the kill-switch off, wait out the daily cap) and retry —
    it should NOT create a partial run row just to record that the
    attempt was blocked (the audit log carries that).

    Idempotency (A5)
    ----------------
    If ``body.idempotency_key`` is set AND a run already exists for
    ``(user_id, key)``, we return that existing run's id with
    ``replayed=True`` — NO pre-flight gate check, NO audit entry,
    NO new row. This matches the idempotency contract most APIs use
    (Stripe, etc.): a retry sees the same response as the original.

    The pre-flight gates only run when we're *actually* creating a
    new row. Running them on a replay would produce a confusing UX:
    the operator would get a 400 "cap hit" response for an idempotent
    retry of a request that originally succeeded.
    """
    # ── Idempotency replay path ────────────────────────────────────
    # Check this FIRST — before any gate — so a retry after a transient
    # network error returns the original run_id instead of spuriously
    # failing on "daily cap hit" from the row the original request
    # already created.
    if body.idempotency_key:
        existing = (await db.execute(
            select(RoutineRun).where(
                RoutineRun.user_id == user.id,
                RoutineRun.idempotency_key == body.idempotency_key,
            )
        )).scalar_one_or_none()
        if existing:
            return CreateRoutineRunResponse(run_id=existing.id, replayed=True)

    if await _kill_switch_disabled(db, user.id):
        raise HTTPException(
            status_code=400,
            detail="Routine is currently disabled by the kill-switch. "
                   "Toggle it off via POST /routine/kill-switch before starting a run.",
        )

    if not await _required_coverage_complete(db, user.id):
        raise HTTPException(
            status_code=400,
            detail="Answer book required-coverage is incomplete. "
                   "Fill all required entries via /answer-book/required-setup.",
        )

    # Daily cap is only a hard block for `live` runs. Dry-runs and
    # single-trial runs don't increment the applied count, so letting
    # them start is safe even when the cap is used.
    if body.mode == "live":
        used_today = await _count_recent_submissions(db, user.id, hours=24)
        if used_today >= DAILY_CAP:
            raise HTTPException(
                status_code=400,
                detail=f"Daily application cap ({DAILY_CAP}/day) already used. "
                       f"Try again in 24h or run in dry_run / single_trial mode.",
            )

    run = RoutineRun(
        id=uuid.uuid4(),
        user_id=user.id,
        mode=body.mode,
        status="running",
        idempotency_key=body.idempotency_key,
    )
    db.add(run)
    try:
        await db.commit()
    except IntegrityError:
        # Race: two concurrent requests with the same idempotency_key
        # both passed the replay check, one committed first. Roll back,
        # re-query, and return the winner's id. This is the only way
        # to honor the idempotency contract under true concurrency.
        await db.rollback()
        if body.idempotency_key:
            winner = (await db.execute(
                select(RoutineRun).where(
                    RoutineRun.user_id == user.id,
                    RoutineRun.idempotency_key == body.idempotency_key,
                )
            )).scalar_one_or_none()
            if winner:
                return CreateRoutineRunResponse(run_id=winner.id, replayed=True)
        # Not an idempotency collision — re-raise as a generic 500.
        raise HTTPException(
            status_code=500,
            detail="Failed to create routine run (integrity error).",
        )
    await db.refresh(run)

    await log_action(
        db, user,
        action="routine.run_created",
        resource="routine_run",
        request=request,
        metadata={
            "run_id": str(run.id),
            "mode": body.mode,
            "target_job_count": len(body.target_job_ids) if body.target_job_ids else 0,
            "idempotency_key": body.idempotency_key,
        },
    )

    return CreateRoutineRunResponse(run_id=run.id, replayed=False)


@router.patch("/runs/{run_id}", response_model=RoutineRunOut)
async def update_run(
    run_id: UUID,
    body: UpdateRoutineRunRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Incremental update of a run's counters / status.

    The routine PATCHes this between applications with partial bodies
    — e.g. ``{"applications_attempted": 3}`` after a skip, or
    ``{"status": "complete", "ended_at": <now>}`` at the end.

    Non-terminal → terminal status transitions auto-stamp ``ended_at``
    if the caller forgets to send it.

    Audit (B4): two kinds of transitions leave an audit row so the
    operator can reconstruct "why did this run stop" from the audit
    log alone:
      * ``kill_switch_triggered`` flipping False→True (mid-run kill)
      * ``status`` transitioning to a terminal value (complete / aborted)

    Counter bumps (applications_attempted etc.) are NOT audited — they
    tick every few seconds during a live run and would drown the log.
    """
    run = (await db.execute(
        select(RoutineRun).where(
            RoutineRun.id == run_id,
            RoutineRun.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Routine run not found")

    # Reject writes on a terminal run — once it's complete/aborted,
    # the counters are frozen. Silent accept would hide routine bugs.
    if run.status in ("complete", "aborted") and body.status is None:
        raise HTTPException(
            status_code=400,
            detail=f"Run is in terminal status '{run.status}' and cannot be updated.",
        )

    patch = body.model_dump(exclude_unset=True)

    # Capture pre-patch state so we can detect transitions before
    # flushing. Tuple order is stable across this function.
    prev_status = run.status
    prev_kill = run.kill_switch_triggered

    if "applications_attempted" in patch and patch["applications_attempted"] is not None:
        run.applications_attempted = patch["applications_attempted"]
    if "applications_submitted" in patch and patch["applications_submitted"] is not None:
        run.applications_submitted = patch["applications_submitted"]
    if "applications_skipped" in patch and patch["applications_skipped"] is not None:
        run.applications_skipped = patch["applications_skipped"]
    if "detection_incidents" in patch and patch["detection_incidents"] is not None:
        run.detection_incidents = patch["detection_incidents"]
    if "kill_switch_triggered" in patch and patch["kill_switch_triggered"] is not None:
        run.kill_switch_triggered = patch["kill_switch_triggered"]
    if "status" in patch and patch["status"] is not None:
        if patch["status"] not in ROUTINE_STATUSES:
            # Belt-and-suspenders — the Literal should already have
            # rejected this at parse time.
            raise HTTPException(status_code=400, detail="Invalid status value")
        run.status = patch["status"]
        if patch["status"] in ("complete", "aborted") and run.ended_at is None:
            run.ended_at = datetime.now(timezone.utc)
    if "ended_at" in patch and patch["ended_at"] is not None:
        run.ended_at = patch["ended_at"]

    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Audit transitions AFTER commit — a failed audit write (fail-open
    # per log_action contract) must not roll back the legitimate patch.
    if prev_kill is False and run.kill_switch_triggered is True:
        await log_action(
            db, user,
            action="routine.run_killed_mid_flight",
            resource="routine_run",
            request=request,
            metadata={
                "run_id": str(run.id),
                "mode": run.mode,
                "applications_submitted": run.applications_submitted,
                "applications_attempted": run.applications_attempted,
            },
        )
    if prev_status == "running" and run.status in ("complete", "aborted"):
        await log_action(
            db, user,
            action=(
                "routine.run_completed" if run.status == "complete"
                else "routine.run_aborted"
            ),
            resource="routine_run",
            request=request,
            metadata={
                "run_id": str(run.id),
                "mode": run.mode,
                "applications_submitted": run.applications_submitted,
                "applications_attempted": run.applications_attempted,
                "applications_skipped": len(run.applications_skipped or []),
                "detection_incidents": len(run.detection_incidents or []),
                "kill_switch_triggered": run.kill_switch_triggered,
            },
        )

    return _run_to_out(run)


@router.get("/runs/{run_id}", response_model=RoutineRunDetail)
async def get_run(
    run_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run detail + every submission recorded during the run.

    Submissions are hydrated from ``application_submissions`` via
    ``routine_run_id`` (SET NULL on run-delete, but we don't delete
    runs so the link is stable). Ordered by submission creation time
    so the UI can render "first app submitted at T=0, last at T=18m".
    """
    run = (await db.execute(
        select(RoutineRun).where(
            RoutineRun.id == run_id,
            RoutineRun.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Routine run not found")

    submissions = (await db.execute(
        select(ApplicationSubmission)
        .where(ApplicationSubmission.routine_run_id == run_id)
        .order_by(ApplicationSubmission.created_at.asc())
    )).scalars().all()

    submission_details = [
        SubmissionDetail(
            id=s.id,
            application_id=s.application_id,
            routine_run_id=s.routine_run_id,
            submitted_at=s.submitted_at,
            job_url=s.job_url,
            ats_platform=s.ats_platform,
            form_fingerprint_hash=s.form_fingerprint_hash,
            payload_json=s.payload_json or {},
            answers_json=s.answers_json or [],
            resume_version_hash=s.resume_version_hash,
            cover_letter_text=s.cover_letter_text,
            screenshot_keys=s.screenshot_keys or [],
            confirmation_text=s.confirmation_text,
            detected_issues=s.detected_issues or [],
            profile_snapshot=s.profile_snapshot or {},
            created_at=s.created_at,
        )
        for s in submissions
    ]

    base = _run_to_out(run)
    return RoutineRunDetail(
        **base.model_dump(),
        submissions=submission_details,
    )


@router.get("/runs", response_model=list[RoutineRunOut])
async def list_runs(
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Most-recent runs first. Simple list — pagination would be
    overkill given the expected volume (a heavy user maybe has 1
    run/day, so 100 rows is ~3 months of history)."""
    rows = (await db.execute(
        select(RoutineRun)
        .where(RoutineRun.user_id == user.id)
        .order_by(RoutineRun.started_at.desc())
        .limit(limit)
    )).scalars().all()
    return [_run_to_out(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════
# Humanize helper
# ═══════════════════════════════════════════════════════════════════


@router.post("/humanize", response_model=HumanizeResponse)
async def humanize(
    body: HumanizeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run text through the humanizer pipeline.

    The routine calls this on every LLM-generated cover letter and
    every generated answer right before it types the text into the
    form. We load the caller's ``humanization_corpus`` rows and pass
    them as few-shot examples so the style-match pass has user-voice
    data to work from.

    Corpus loading rules (kept here, not in the humanizer module, so
    the module stays DB-free and unit-testable):

      * Only rows with ``edit_distance > 10`` — small edits are
        noise; we want examples where the user meaningfully rewrote.
      * Most recent 100 rows (``accepted_at`` DESC) — the user's
        voice drifts; stale corpus can over-fit to old phrasing.
      * Scoped to ``promoted_to_answer_book = False`` — promoted
        rows are already in the answer book; reusing them as
        style-match examples risks the model regenerating an
        answer we could have just read from the book.

    When the corpus is below the style-match threshold, the pass
    no-ops (see ``humanizer.style_match_pass``) — that's fine; the
    other 7 passes still run.
    """
    corpus_rows = (await db.execute(
        select(HumanizationCorpus)
        .where(
            HumanizationCorpus.user_id == user.id,
            HumanizationCorpus.edit_distance > 10,
            HumanizationCorpus.promoted_to_answer_book == False,  # noqa: E712
        )
        .order_by(HumanizationCorpus.accepted_at.desc())
        .limit(100)
    )).scalars().all()

    examples: list[tuple[str, str]] = [
        (r.draft_text, r.final_text) for r in corpus_rows
    ]

    result = humanize_text(body.text, corpus_examples=examples or None)

    return HumanizeResponse(
        text=result.text,
        passes_applied=result.passes_applied,
        burstiness_sigma=result.burstiness_sigma,
        banned_phrase_hits=result.banned_phrase_hits,
        style_match_examples_used=result.style_match_examples_used,
    )
