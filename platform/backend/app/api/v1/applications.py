"""Application tracking endpoints."""

import uuid
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.application import Application
from app.models.job import Job
from app.models.company import Company, CompanyATSBoard
from app.models.resume import Resume, ResumeScore
from app.models.answer_book import AnswerBookEntry
from app.models.pipeline_stage import PipelineStage
from app.models.platform_credential import PlatformCredential
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.utils.audit import log_action
from app.utils.sql import escape_like

# F261 — admin gate for the team-pipeline endpoints. ``require_role(
# "admin")`` admits both admin and super_admin via the role hierarchy
# in app/api/deps.py. Reviewers + viewers see only their own
# applications via the existing user-scoped routes.
_TEAM_PIPELINE_GUARD = require_role("admin")

router = APIRouter(prefix="/applications", tags=["applications"])

# Valid status transitions -- terminal states have no outgoing transitions
#
# F347 adds the three server-side-apply states. They sit *before*
# "submitted" in the lifecycle and exist because unattended submission
# has outcomes the old vocabulary couldn't express:
#
#   in_flight  : the worker is driving the ATS form right now. Transient;
#                the worker always moves it on. A row stuck here means a
#                worker died mid-submit — see the sweeper in F347.
#   needs_user : the apply gate refused. NOT a failure — we understood
#                the form well enough to know we shouldn't answer it
#                unattended (a legal question with no saved answer, a
#                form we couldn't extract, a CAPTCHA). Only a human
#                clears it, so retrying is pointless.
#   failed     : we tried and the attempt errored. Retryable, and the
#                task does retry with backoff before landing here.
#
# `needs_user` and `failed` both allow "applied" so a user who finishes
# the application by hand can mark it done, matching the escape hatch
# Tsenta offers ("Skip" / "I've Applied").
VALID_TRANSITIONS = {
    "prepared": ["in_flight", "needs_user", "applied", "withdrawn"],
    "in_flight": ["submitted", "needs_user", "failed", "withdrawn"],
    "needs_user": ["in_flight", "submitted", "applied", "withdrawn"],
    "failed": ["in_flight", "needs_user", "applied", "withdrawn"],
    "submitted": ["applied", "withdrawn"],
    "applied": ["interview", "rejected", "withdrawn"],
    "interview": ["offer", "rejected", "withdrawn"],
    "offer": ["rejected", "withdrawn"],
    "rejected": [],
    "withdrawn": [],
}

# Regression finding 194: PATCH /applications/{id} was declared with
# `body: dict`, so any stray key (typo like `stauts`, camelCase
# `preparedAnswers`, or a hand-crafted `{"__evil__": "<script>..."}`)
# was silently ignored with a 200 response. Users thought they had
# advanced the application's stage when nothing changed. We now parse
# against a strict schema: `status` is a Literal over the documented
# states, and `extra="forbid"` causes Pydantic v2 to 422 on any
# unknown field. The VALID_TRANSITIONS state-machine check still runs
# against the parsed `status` value since it depends on the row's
# current state (not expressible in a static schema).
ApplicationStatus = Literal[
    "prepared", "submitted", "applied", "interview",
    "offer", "rejected", "withdrawn",
    # F347 server-side apply states. Listed here as well as in
    # VALID_TRANSITIONS because this Literal types the `status` query
    # param on the list endpoints — without them `?status=needs_user`
    # 422s and the review queue can't load its own rows.
    "in_flight", "needs_user", "failed",
]


# Regression finding 129: `prepared_answers` was `list[dict] | None =
# None` with no per-item shape and no array cap, so a client could
# POST 10,000 dicts × 100-char answer strings (1.37 MB parsed) and
# have it persisted on `Application.prepared_answers` (typed JSON,
# unbounded). On a user with 100 apps, that's ~137 MB per user in
# storage, plus every analytics endpoint that joins `Application`
# loads the blob into memory. Same failure-mode class as F130 (unbounded
# comment), F131 (unbounded description), F80 (answer book). Per-item
# ApplicationAnswer schema caps the two free-text fields; `max_length=
# 200` on the outer array is ~10× a real ATS form (typical 5-30 fields).
class ApplicationAnswer(BaseModel):
    """One field of a prepared-application answer payload.

    Mirrors the shape the `/applications/prepare` handler builds at
    line 285 — only the free-text fields get length-capped here; the
    rest are enums / bools / question_keys that are already length-
    bounded by the ATS form they came from.
    """

    model_config = ConfigDict(extra="allow")

    # `question_key` is derived from the ATS form schema and typically
    # reads like `first_name`, `years_experience`, `cover_letter`.
    # 200 chars is 5x the longest question_key the repo generates
    # today, with room for provider-prefixed keys like
    # `greenhouse_custom_field_1234`.
    question_key: str | None = Field(default=None, max_length=200)
    # `answer` is the actual user-entered value. Most answers are
    # short (name, email, yes/no); cover-letter fields can go long but
    # 5000 chars is a hard upper bound (typical cover letter is
    # ~300 words ≈ 2000 chars). Above this we're clearly being
    # DoS-payload'd, not getting real data.
    answer: str | None = Field(default=None, max_length=5000)


# F298 — Pydantic schemas for the last two ``body: dict`` handlers
# in this file. Same F128/F194 motivation as ApplicationUpdate
# above: untyped ``dict`` lets typos sail through (``jb_id``
# instead of ``job_id``) and unknown fields silently drop, masking
# admin keystroke errors as no-op responses.
class PrepareApplicationRequest(BaseModel):
    """Body for ``POST /applications/prepare``."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID


class RecordApplicationRequest(BaseModel):
    """Body for ``POST /applications/record``.

    The browser-side apply lane (the ``job-apply`` Claude skill under
    ``.claude/skills/``) fills the ATS form in the user's own Chrome and
    needs to store what it typed against this job's Application so the
    answers are recallable from the platform. ``/prepare`` can't serve
    that lane: it demands a stored ``PlatformCredential`` for the ATS
    (built for the old server-side login flow) and fetches the questions
    itself rather than accepting what the operator actually entered.

    Exactly one of ``job_id`` / ``job_url`` must be set — the skill has
    the id when targets came from ``GET /jobs``, but only the URL when
    the user pasted a posting. ``answers`` reuse ``ApplicationAnswer``
    (``extra="allow"``) so the skill can send ``label`` / ``field_type``
    and the handler fills the rest of the ``PreparedAnswer`` shape the
    Job Detail page already renders. Same 200-item cap as
    ``ApplicationUpdate``.
    """
    model_config = ConfigDict(extra="forbid")
    job_id: UUID | None = None
    job_url: str | None = Field(default=None, min_length=1, max_length=2000)
    answers: list[ApplicationAnswer] = Field(default_factory=list, max_length=200)
    notes: str = Field(default="", max_length=5000)
    ats_platform: str | None = Field(default=None, max_length=50)


_RECORD_FIELD_TYPES = {"text", "textarea", "select", "multi_select", "file", "boolean"}


def _normalize_recorded_answers(answers: list[dict]) -> list[dict]:
    """Coerce operator-supplied answers into the ``PreparedAnswer`` shape.

    The frontend (``JobDetailPage`` → ``PreparedAnswer``) expects every
    row to carry ``field_key / label / field_type / required / options /
    description / answer / match_source / question_key / confidence``.
    The skill only reliably knows the label it saw and the value it
    typed, so everything else gets a sensible default: ``match_source=
    "override"`` (the operator chose it, not the matcher) and
    ``confidence="high"``. ``question_key`` falls back to the normalised
    label so ``/sync-answers`` and ``/promote-answer`` can key on it.
    Rows with neither a label nor a key are dropped rather than stored
    as unrecallable blanks.
    """
    from app.services.question_service import _normalise_key

    out: list[dict] = []
    for raw in answers:
        label = (raw.get("label") or raw.get("question") or "").strip()
        qk = (raw.get("question_key") or "").strip() or _normalise_key(label)
        if not qk:
            continue
        ftype = raw.get("field_type") or "text"
        if ftype not in _RECORD_FIELD_TYPES:
            ftype = "text"
        out.append({
            "field_key": (raw.get("field_key") or qk)[:255],
            "label": label or qk,
            "field_type": ftype,
            "required": bool(raw.get("required", False)),
            "options": raw.get("options") or [],
            "description": raw.get("description") or "",
            "answer": raw.get("answer") or "",
            "match_source": "override",
            "question_key": qk[:200],
            "confidence": "high",
        })
    return out


class SyncAnswerItem(BaseModel):
    """One ``{question_key, answer}`` row for ``/sync-answers``."""

    model_config = ConfigDict(extra="forbid")

    # Same caps as ``PreparedAnswer`` above so the two pathways
    # share a single contract for "what fits in an answer cell".
    question_key: str = Field(..., min_length=1, max_length=200)
    answer: str = Field(default="", max_length=5000)


class SyncAnswersRequest(BaseModel):
    """Body for ``POST /applications/{id}/sync-answers``.

    ``answers`` is the list of edits to fold back into the
    answer_book. The 200-item cap matches the worst-case ATS form
    we've observed (~120 questions); anything larger is a DoS
    payload, not legitimate data.
    """

    model_config = ConfigDict(extra="forbid")

    answers: list[SyncAnswerItem] = Field(default_factory=list, max_length=200)


class ApplicationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApplicationStatus | None = None
    # F129: 5000-char cap matches the answer_book + cover_letter
    # precedent. Pre-fix was 10000 (F194) which rejected the 10MB
    # probe but still allowed ~40KB of prose per row. Tightening here
    # now that we know the real-world upper bound. Rows with >5000
    # chars written before this fix are grandfathered (PATCH only
    # rejects oversize writes; reads still return whatever's there).
    notes: str | None = Field(default=None, max_length=5000)
    # F129: cap the outer list at 200 + enforce ApplicationAnswer on
    # each item. `extra="allow"` on the inner schema keeps backward
    # compatibility with any provider-specific fields the
    # /applications/prepare handler adds (confidence, match_source,
    # field_type, etc.) — those aren't free-text user input so they
    # don't need capping.
    prepared_answers: list[ApplicationAnswer] | None = Field(default=None, max_length=200)


def credentials_required(platform: str) -> bool:
    """Does applying on this platform need a stored ATS login?

    F369. The ``PlatformCredential`` gate on ``/prepare`` and
    ``/readiness`` was built for the old server-side *login* flow. The
    server-side submitters (F347+) drive the public application form —
    Greenhouse, Recruitee, Workable and Ashby forms have no account —
    so for those platforms the gate only ever blocked. Found on
    production: every Ashby job returned "Platform credentials required
    for ashby" from /prepare, and the job page's Apply button was
    disabled by the same rule, so the F368 adapter was unreachable.
    """
    from app.services.submitters import auto_submittable_platforms

    return (platform or "").strip().lower() not in auto_submittable_platforms()


@router.get("/readiness/{job_id}")
async def get_apply_readiness(
    job_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if user is ready to apply for a job. Returns readiness status for each prerequisite."""
    # Load job
    job = (await db.execute(
        select(Job).where(Job.id == job_id)
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Resume check
    resume_ready = False
    resume_info = None
    if user.active_resume_id:
        resume = (await db.execute(
            select(Resume).where(Resume.id == user.active_resume_id, Resume.user_id == user.id)
        )).scalar_one_or_none()
        if resume and resume.status == "ready":
            resume_ready = True
            resume_info = {"id": str(resume.id), "label": resume.label or resume.filename}

    # Credential check — only for platforms whose flow needs a login.
    cred_required = credentials_required(job.platform)
    cred_ready = not cred_required
    cred_info = None
    if resume_ready and cred_required:
        cred = (await db.execute(
            select(PlatformCredential).where(
                PlatformCredential.resume_id == user.active_resume_id,
                PlatformCredential.platform == job.platform,
            )
        )).scalar_one_or_none()
        if cred and cred.encrypted_password:
            cred_ready = True
            cred_info = {"platform": cred.platform, "email": cred.email}

    # Answer book count
    ab_count_q = select(func.count(AnswerBookEntry.id)).where(
        AnswerBookEntry.user_id == user.id,
        or_(
            AnswerBookEntry.resume_id.is_(None),
            AnswerBookEntry.resume_id == user.active_resume_id,
        ),
    )
    ab_count = (await db.execute(ab_count_q)).scalar() or 0

    # Resume score.
    # resume_scores has no UNIQUE (resume_id, job_id); duplicate rows
    # happen in production (concurrent scoring tasks) and would make
    # `scalar_one_or_none()` raise `MultipleResultsFound` → 500. Pick
    # the most recent row until the dedupe + UNIQUE migration lands.
    score_info = None
    if resume_ready:
        score = (await db.execute(
            select(ResumeScore.overall_score)
            .where(
                ResumeScore.resume_id == user.active_resume_id,
                ResumeScore.job_id == job.id,
            )
            .order_by(ResumeScore.scored_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if score is not None:
            score_info = {"score": round(score, 1)}

    # Existing application check
    existing = (await db.execute(
        select(Application.id, Application.status).where(
            Application.user_id == user.id,
            Application.job_id == job.id,
        )
    )).first()

    return {
        "resume": {"ready": resume_ready, **(resume_info or {})},
        "credentials": {"ready": cred_ready, "required": cred_required, "platform": job.platform, **(cred_info or {})},
        "answer_book": {"ready": ab_count > 0, "count": ab_count},
        "resume_score": {"available": score_info is not None, **(score_info or {})},
        "existing_application": {"exists": existing is not None, "id": str(existing[0]) if existing else None, "status": existing[1] if existing else None},
        "can_apply": resume_ready and cred_ready,
    }


class FromUrlRequest(BaseModel):
    """Body for ``POST /applications/from-url`` (F371, bring your own link)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=2000)


@router.post("/from-url")
async def application_from_url(
    body: FromUrlRequest,
    user: User = Depends(get_current_user),
):
    """Resolve a pasted posting URL into a Job the review queue can use.

    F371. Returns the job (creating it from the ATS board when we never
    scanned it) plus what we can do with it, so the UI can go straight
    to ``/prepare`` and the review page. Refuses, with the reason and
    without creating anything, when the link isn't a posting on an ATS
    we can read — the Tsenta failure this exists to not repeat.
    """
    import asyncio

    from app.fetchers.questions import human_wall_for
    from app.services.own_link import OwnLinkError, resolve_job_from_url
    from app.services.submitters import auto_submittable_platforms

    try:
        resolved = await asyncio.to_thread(resolve_job_from_url, body.url)
    except OwnLinkError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return {
        "job_id": resolved.job_id,
        "platform": resolved.platform,
        "slug": resolved.slug,
        "external_id": resolved.external_id,
        "title": resolved.title,
        "company_name": resolved.company_name,
        "url": resolved.url,
        "created": resolved.created,
        "auto_submittable": resolved.platform in auto_submittable_platforms(),
        "wall": human_wall_for(resolved.platform),
    }


@router.post("/prepare")
async def prepare_application(
    body: PrepareApplicationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Prepare an application for a job using the active resume.

    Fetches the actual ATS form questions from the platform, matches
    them against the user's answer book, and returns a structured list
    of fields with pre-filled answers.

    F298: ``body: dict`` was the F128 pattern — typos like ``jb_id``
    silently dropped and the handler returned 400 "job_id is
    required". Pydantic now 422s the bad input at parse time with
    a useful field-level error.
    """
    # F298: pydantic enforces presence + UUID type at parse time;
    # the manual ``if not job_id`` guard is no longer needed.
    job_id = body.job_id

    if not user.active_resume_id:
        raise HTTPException(status_code=400, detail="No active resume selected. Please switch to a resume first.")

    # Load resume
    resume = (await db.execute(
        select(Resume).where(Resume.id == user.active_resume_id, Resume.user_id == user.id)
    )).scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Active resume not found")

    # Load job with company
    job = (await db.execute(
        select(Job).options(joinedload(Job.company)).where(Job.id == job_id)
    )).unique().scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Enforce credential requirement — F369: not on platforms the
    # server-side submitters drive, whose public forms have no login.
    if credentials_required(job.platform):
        credential = (await db.execute(
            select(PlatformCredential).where(
                PlatformCredential.resume_id == resume.id,
                PlatformCredential.platform == job.platform,
            )
        )).scalar_one_or_none()
        if not credential or not credential.encrypted_password:
            raise HTTPException(
                status_code=400,
                detail=f"Platform credentials required for {job.platform}. Add credentials before applying.",
            )

    # Check if application already exists
    existing = (await db.execute(
        select(Application).where(
            Application.user_id == user.id,
            Application.job_id == job.id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Application already exists for this job")

    # Look up board slug for question fetching
    board = (await db.execute(
        select(CompanyATSBoard).where(
            CompanyATSBoard.company_id == job.company_id,
            CompanyATSBoard.platform == job.platform,
            CompanyATSBoard.is_active.is_(True),
        )
    )).scalar_one_or_none()
    board_slug = board.slug if board else ""

    # Fetch ATS form questions (cached via question service)
    from app.services.question_service import get_or_fetch_questions, auto_populate_answer_book
    ats_questions = await get_or_fetch_questions(db, job, board_slug)
    new_entries = await auto_populate_answer_book(db, user.id, ats_questions)

    # Load and merge answer book entries
    entries = (await db.execute(
        select(AnswerBookEntry).where(
            AnswerBookEntry.user_id == user.id,
            or_(
                AnswerBookEntry.resume_id.is_(None),
                AnswerBookEntry.resume_id == resume.id,
            ),
        ).order_by(AnswerBookEntry.category)
    )).scalars().all()

    merged: dict[str, dict] = {}
    for entry in entries:
        key = entry.question_key
        if key not in merged or entry.resume_id is not None:
            merged[key] = {
                "question_key": entry.question_key,
                "question": entry.question,
                "answer": entry.answer,
                "category": entry.category,
                "source": "override" if entry.resume_id else "base",
            }

    # Match ATS questions to answer book
    from app.workers.tasks._answer_prep import match_questions_to_answers
    prepared_answers = match_questions_to_answers(ats_questions, list(merged.values()))

    # Get resume score for this job.
    # See the matching comment above: pick the most recently computed
    # score to avoid MultipleResultsFound when the table has duplicate
    # rows for the (resume_id, job_id) pair.
    score = (await db.execute(
        select(ResumeScore.overall_score)
        .where(
            ResumeScore.resume_id == resume.id,
            ResumeScore.job_id == job.id,
        )
        .order_by(ResumeScore.scored_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    # F321 (data correctness, idempotent + race-safe): the Application
    # table has UNIQUE(user_id, job_id) so a second ``/prepare`` click
    # on the same job pre-fix raised IntegrityError → 500. Two
    # behaviours to preserve:
    #
    #   (a) **Idempotent re-prepare**: a user clicking Prepare twice
    #       on the same job should NOT 500. Refresh ``prepared_answers``
    #       + ``resume_id`` on the existing row and return it.
    #
    #   (b) **Don't downgrade an already-applied row**: if the user
    #       had moved past prepared (status='applied'/interview/offer),
    #       a fresh /prepare click shouldn't reset the lifecycle.
    #       Return a 409 telling the client to use /status PATCH if
    #       they want to re-prepare a previously-applied job.
    #
    # F261: denormalise company_id at apply-time so the team-pipeline
    # feed doesn't pay an Application⨝Job join on every page-load.
    existing = (await db.execute(
        select(Application).where(
            Application.user_id == user.id,
            Application.job_id == job.id,
        )
    )).scalar_one_or_none()

    if existing is not None:
        if existing.status != "prepared":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This job already has a {existing.status!r} "
                    f"application — use PATCH /applications/{existing.id} "
                    f"to update or DELETE to withdraw before re-preparing."
                ),
            )
        # (a) idempotent re-prepare path: refresh answers + resume
        existing.prepared_answers = prepared_answers
        existing.resume_id = resume.id
        await db.commit()
        await db.refresh(existing)
        application = existing
    else:
        application = Application(
            id=uuid.uuid4(),
            user_id=user.id,
            job_id=job.id,
            company_id=job.company_id,
            resume_id=resume.id,
            status="prepared",
            apply_method="manual_copy",
            prepared_answers=prepared_answers,
        )
        db.add(application)
        # SAVEPOINT + IntegrityError catch closes the residual race
        # where two requests pass the SELECT then both reach INSERT.
        try:
            async with db.begin_nested():
                await db.flush()
        except IntegrityError:
            # Lost the race; re-fetch the winner and refresh into it
            # (matches branch (a) semantics).
            db.expunge(application)
            existing = (await db.execute(
                select(Application).where(
                    Application.user_id == user.id,
                    Application.job_id == job.id,
                )
            )).scalar_one_or_none()
            if existing is None:
                raise
            if existing.status != "prepared":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"This job already has a {existing.status!r} "
                        f"application — use PATCH /applications/{existing.id} "
                        f"to update or DELETE to withdraw before re-preparing."
                    ),
                )
            existing.prepared_answers = prepared_answers
            existing.resume_id = resume.id
            application = existing
        await db.commit()
        await db.refresh(application)

    return {
        "id": str(application.id),
        "job": {
            "id": str(job.id),
            "title": job.title,
            "company_name": job.company.name if job.company else "",
            "platform": job.platform,
            "url": job.url,
        },
        "resume": {
            "id": str(resume.id),
            "label": resume.label or resume.filename,
        },
        "resume_score": round(score, 1) if score else None,
        "apply_method": "manual_copy",
        "has_credentials": True,
        "prepared_answers": prepared_answers,
        "status": "prepared",
    }


@router.post("/record")
async def record_application(
    body: RecordApplicationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create (or refresh) the Application for a browser-side apply.

    Called by the ``job-apply`` skill right after it has filled the ATS
    form and BEFORE the user approves the submit, so the exact answers
    typed are on record even if the submit is later skipped. After a
    confirmed submit the skill calls ``/{id}/confirm-submitted``, which
    owns the ``applied`` transition and all its side effects — this
    endpoint deliberately never sets a status past ``prepared``.

    Upsert rules: no row → create. Existing row still ``prepared`` →
    overwrite answers/notes (the skill re-fills after an edit). Existing
    row in any later status → 409, so a re-run can't clobber the history
    of something already submitted.

    ``resume_id`` is NOT NULL on the model, so the user's active resume
    is required — same rule ``/prepare`` applies, same error text.
    """
    if (body.job_id is None) == (body.job_url is None):
        raise HTTPException(status_code=400, detail="Provide exactly one of job_id or job_url.")
    if not user.active_resume_id:
        raise HTTPException(status_code=400, detail="No active resume selected. Please switch to a resume first.")

    if body.job_id is not None:
        job = (await db.execute(select(Job).where(Job.id == body.job_id))).scalar_one_or_none()
    else:
        url = body.job_url.strip()
        job = (await db.execute(
            select(Job).where(Job.url == url).order_by(Job.first_seen_at.desc())
        )).scalars().first()
        if not job:
            # Boards append tracking params (``?gh_src=``, ``?lever-source=``);
            # match on the path when the exact URL misses.
            base = url.split("?", 1)[0].rstrip("/")
            job = (await db.execute(
                select(Job).where(Job.url.like(escape_like(base) + "%")).order_by(Job.first_seen_at.desc())
            )).scalars().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found in the platform for the given id/url.")

    answers = _normalize_recorded_answers([a.model_dump() for a in body.answers])
    notes = body.notes
    if body.ats_platform and body.ats_platform not in notes:
        notes = f"[{body.ats_platform}] {notes}".strip()

    existing = (await db.execute(
        select(Application).where(Application.user_id == user.id, Application.job_id == job.id)
    )).scalar_one_or_none()
    created = False
    if existing:
        if existing.status != "prepared":
            raise HTTPException(
                status_code=409,
                detail=f"This job already has a {existing.status!r} application; not overwriting its answers.",
            )
        existing.prepared_answers = answers
        existing.notes = notes
        existing.apply_method = "claude_routine"
        existing.submission_source = "routine"
        app = existing
    else:
        app = Application(
            id=uuid.uuid4(),
            user_id=user.id,
            job_id=job.id,
            resume_id=user.active_resume_id,
            company_id=job.company_id,
            status="prepared",
            apply_method="claude_routine",
            submission_source="routine",
            prepared_answers=answers,
            notes=notes,
        )
        db.add(app)
        created = True

    await log_action(
        db, user,
        action="application.recorded",
        resource="application",
        metadata={
            "application_id": str(app.id),
            "job_id": str(job.id),
            "answer_count": len(answers),
            "created": created,
        },
    )
    try:
        await db.commit()
    except IntegrityError:
        # Two skill tabs racing on the same job — the unique (user, job)
        # constraint wins; surface it as the same 409 the slow path gives.
        await db.rollback()
        raise HTTPException(status_code=409, detail="Application already exists for this job.")

    return {
        "id": str(app.id),
        "application_id": str(app.id),
        "job_id": str(job.id),
        "job_url": job.url,
        "status": app.status,
        "created": created,
        "prepared_answers": app.prepared_answers,
    }


@router.post("/{app_id}/sync-answers")
async def sync_answers_to_book(
    app_id: UUID,
    body: SyncAnswersRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sync edited answers back to the answer book.

    Accepts a list of {question_key, answer} and updates matching
    AnswerBookEntry records, preferring resume-specific entries.

    F298: ``body: dict`` + ``item.get(...)`` pattern was untyped.
    Now uses ``SyncAnswersRequest`` with ``extra="forbid"``,
    bounded list size, and per-item field validation. Empty
    question_keys 422 at parse time instead of being silently
    skipped.
    """
    app = (await db.execute(
        select(Application).where(Application.id == app_id, Application.user_id == user.id)
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    if not body.answers:
        return {"synced": 0}

    synced = 0
    for item in body.answers:
        qk = item.question_key.strip()
        answer_text = item.answer
        if not qk:
            continue

        # Try resume-specific entry first, then base
        entry = (await db.execute(
            select(AnswerBookEntry).where(
                AnswerBookEntry.user_id == user.id,
                AnswerBookEntry.resume_id == app.resume_id,
                AnswerBookEntry.question_key == qk,
            )
        )).scalar_one_or_none()

        if not entry:
            entry = (await db.execute(
                select(AnswerBookEntry).where(
                    AnswerBookEntry.user_id == user.id,
                    AnswerBookEntry.resume_id.is_(None),
                    AnswerBookEntry.question_key == qk,
                )
            )).scalar_one_or_none()

        if entry:
            entry.answer = answer_text
            entry.usage_count = (entry.usage_count or 0) + 1
            db.add(entry)
            synced += 1

    await db.commit()
    return {"synced": synced}


@router.get("/by-job/{job_id}")
async def get_application_by_job(
    job_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get existing application for a specific job (if any)."""
    result = await db.execute(
        select(Application)
        .options(joinedload(Application.job), joinedload(Application.resume))
        .where(Application.user_id == user.id, Application.job_id == job_id)
    )
    app = result.unique().scalar_one_or_none()
    if not app:
        return None

    return {
        "id": str(app.id),
        "job_id": str(app.job_id),
        "resume_id": str(app.resume_id),
        "resume_label": (app.resume.label or app.resume.filename) if app.resume else "",
        "status": app.status,
        "apply_method": app.apply_method,
        "prepared_answers": app.prepared_answers or [],
        "notes": app.notes or "",
        "applied_at": app.applied_at.isoformat() if app.applied_at else None,
        "created_at": app.created_at.isoformat(),
    }


@router.get("/stats")
async def get_application_stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get application counts by status."""
    result = await db.execute(
        select(Application.status, func.count(Application.id))
        .where(Application.user_id == user.id)
        .group_by(Application.status)
    )
    counts = {row[0]: row[1] for row in result}

    total = sum(counts.values())
    return {
        "total": total,
        "prepared": counts.get("prepared", 0),
        "submitted": counts.get("submitted", 0),
        "applied": counts.get("applied", 0),
        "interview": counts.get("interview", 0),
        "offer": counts.get("offer", 0),
        "rejected": counts.get("rejected", 0),
        "withdrawn": counts.get("withdrawn", 0),
    }


# ═══════════════════════════════════════════════════════════════════
# F261 — Team Pipeline Tracker
# ═══════════════════════════════════════════════════════════════════
#
# When the team applies to a job we need a single shared timeline:
# who applied, when, with which resume, to which company, and where
# is each application now in the funnel. The existing per-user list
# at GET /applications doesn't surface other users' applications, so
# when an HR contact replies the team can't easily map the reply
# back to the originating applicant.
#
# Two admin-gated endpoints support the new feed:
#   * GET /applications/team — cross-user list with applicant
#     identity (name, email) denormalised into the row so a recruiter
#     reply can be triaged without a second fetch.
#   * PATCH /applications/{id}/stage — manual funnel-stage move,
#     audited. ``status`` is the apply-state machine (prepared →
#     applied → interview → offer); ``stage_key`` is the configurable
#     pipeline stage (Interview 1, Final round, etc.) admins manage
#     via /pipeline/stages. Auto-advance was explicitly out of scope
#     for v1 — every stage move is a deliberate admin action.
#
# RBAC: ``require_role("admin")`` admits admin + super_admin via the
# role hierarchy. Reviewers and viewers continue to see only their
# own applications via GET /applications (no scope change there).


@router.get("/team")
async def list_team_applications(
    # Reuse the same Literal allow-list as the per-user endpoint so
    # ``?status=Rejected`` (capital R) parse-422s instead of silently
    # returning total=0. F220(B) regression class.
    status: ApplicationStatus | None = None,
    # ``stage_key`` filters by the configurable pipeline stage. Free-
    # form string here (no Literal) because pipeline_stages is admin-
    # configurable at runtime; we soft-validate against the table on
    # PATCH but accept any value here so a deactivated stage's
    # historical rows remain queryable.
    stage_key: str | None = None,
    # Filter by company so the side-panel under a pipeline card can
    # call ``GET /applications/team?company_id=...`` directly.
    company_id: UUID | None = None,
    # Filter by applicant — admin-only "show me Sarthak's pipeline"
    # view. Frontend feeds this from the user-management list.
    user_id: UUID | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: User = Depends(_TEAM_PIPELINE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Team-wide application feed (admin / super_admin only).

    Returns one row per Application across every user, joined with
    enough context that a row standing alone tells the full story:
    applicant identity, job, company, resume, stage, and timestamps.
    Pagination + filters mirror the per-user endpoint to keep the
    frontend's filter logic shared.
    """
    # The team-feed query is structurally identical to the per-user
    # query except for (a) no ``user_id == user.id`` filter and (b)
    # an extra User join to surface applicant name/email. We left-
    # join on Company so applications whose Company row was deleted
    # (denormalised company_id became NULL via ON DELETE SET NULL)
    # still surface — they'd otherwise vanish from the feed.
    query = (
        select(
            Application,
            Job,
            Company.name.label("co_name"),
            Resume.label.label("resume_label"),
            Resume.filename.label("resume_filename"),
            User.name.label("applicant_name"),
            User.email.label("applicant_email"),
        )
        .join(Job, Application.job_id == Job.id)
        .join(User, Application.user_id == User.id)
        .join(Resume, Application.resume_id == Resume.id)
        .outerjoin(Company, Application.company_id == Company.id)
    )

    if status:
        query = query.where(Application.status == status)
    if stage_key:
        query = query.where(Application.stage_key == stage_key)
    if company_id:
        query = query.where(Application.company_id == company_id)
    if user_id:
        query = query.where(Application.user_id == user_id)
    if search and search.strip():
        # Findings 84+85: escape LIKE metachars + drop whitespace-only
        # input. Same pattern as the per-user list endpoint.
        term = f"%{escape_like(search.strip())}%"
        query = query.where(or_(
            Job.title.ilike(term, escape="\\"),
            Company.name.ilike(term, escape="\\"),
            User.name.ilike(term, escape="\\"),
            User.email.ilike(term, escape="\\"),
        ))

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Order by most-recent-activity first. ``COALESCE`` so a row with
    # ``applied_at`` set sorts by that; otherwise by created_at.
    query = query.order_by(
        func.coalesce(Application.applied_at, Application.created_at).desc()
    )
    query = query.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).all()

    items = []
    for app, job, co_name, resume_label, resume_filename, applicant_name, applicant_email in rows:
        items.append({
            "id": str(app.id),
            "job_id": str(app.job_id),
            "job_title": job.title,
            "job_url": job.url,
            "platform": job.platform,
            "company_id": str(app.company_id) if app.company_id else None,
            "company_name": co_name or "",
            "user_id": str(app.user_id),
            "applicant_name": applicant_name,
            "applicant_email": applicant_email,
            "resume_id": str(app.resume_id),
            "resume_label": resume_label or resume_filename or "",
            "status": app.status,
            "stage_key": app.stage_key,
            "apply_method": app.apply_method,
            "submission_source": app.submission_source,
            "applied_at": app.applied_at.isoformat() if app.applied_at else None,
            "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
            "created_at": app.created_at.isoformat(),
            "notes": app.notes,
        })

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


class ApplicationStageUpdate(BaseModel):
    """Body for PATCH /applications/{id}/stage. ``stage_key=null``
    clears the stage (e.g. when an admin reverts a wrongly-tagged
    application). Notes optional — useful to log why a stage moved
    backwards (e.g. "candidate ghosted, moving back from Interview 2
    to Applied").
    """

    model_config = ConfigDict(extra="forbid")

    stage_key: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=500)


@router.patch("/{app_id}/stage")
async def update_application_stage(
    app_id: UUID,
    body: ApplicationStageUpdate,
    user: User = Depends(_TEAM_PIPELINE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Manually move an application's funnel stage (admin only).

    Soft-validates ``stage_key`` against the ``pipeline_stages``
    catalog so a typo (``"Inteview 1"``) gets a 400 instead of being
    silently persisted. ``stage_key=null`` is allowed — it clears the
    stage on the application without requiring a catalog lookup.

    Audited as ``application.stage_changed`` so the team can trace
    "who moved this app to Interview 2 last Tuesday." The audit
    metadata captures both the old and new stage so reverts are
    explicit in the log.
    """
    app = (await db.execute(
        select(Application).where(Application.id == app_id)
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    new_stage = body.stage_key
    if new_stage is not None:
        # Catalog check — must reference an active stage. Inactive
        # stages stay queryable on existing rows (so historical
        # reports keep working) but new writes can't pin to one.
        stage_row = (await db.execute(
            select(PipelineStage).where(
                PipelineStage.key == new_stage,
                PipelineStage.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if not stage_row:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown or inactive pipeline stage '{new_stage}'. "
                    "Manage stages at /pipeline/stages."
                ),
            )

    old_stage = app.stage_key
    app.stage_key = new_stage
    await db.commit()

    await log_action(
        db, user,
        action="application.stage_changed",
        resource="application",
        metadata={
            "application_id": str(app.id),
            "old_stage": old_stage,
            "new_stage": new_stage,
            "note": (body.note or "").strip() or None,
        },
    )

    return {
        "id": str(app.id),
        "stage_key": app.stage_key,
        "old_stage": old_stage,
    }


@router.get("")
async def list_applications(
    # Regression finding 220(B): `status` was typed `str | None` while the
    # `ApplicationStatus` Literal (used by `ApplicationUpdate` on line 57)
    # was sitting right there in the same module. A typo like `?status=
    # Rejected` (capital R), `?status=APPLIED` (all caps), or `?status=
    # <script>` silently returned HTTP 200 with total=0 — users saw "no
    # applications" for a valid-looking filter value. Reusing the Literal
    # here gives us a parse-time 422 that enumerates the allowed states.
    # Same bug class as F187 (/export/jobs), F218 (/jobs), and F191
    # (/platforms).
    status: ApplicationStatus | None = None,
    # Regression finding 228: `submission_source` was ADDED as a response
    # field by the Feature C migration (r8m9n0o1p2q3) and consumed by the
    # frontend as a "Source" badge + gating for the "What we sent" modal
    # (ApplicationsPage.tsx:201,262), but the matching INPUT filter was
    # missed. Live verification at deploy showed `?submission_source=
    # review_queue` silently returned total=9 (unfiltered). Same
    # F220(A)/(C) silent-accept class — declared params validate
    # cleanly, undeclared params are discarded by FastAPI without
    # warning. Literal-typing here gives a parse-time 422 on typos and
    # the matching WHERE below makes the filter actually bite.
    submission_source: Literal["review_queue", "manual_prepare"] | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List applications with filters."""
    query = (
        select(Application, Job, Company.name.label("co_name"), Resume.label.label("resume_label"), Resume.filename.label("resume_filename"))
        .join(Job, Application.job_id == Job.id)
        .join(Company, Job.company_id == Company.id)
        .join(Resume, Application.resume_id == Resume.id)
        .where(Application.user_id == user.id)
    )

    if status:
        query = query.where(Application.status == status)
    if submission_source:
        query = query.where(Application.submission_source == submission_source)
    if search and search.strip():
        # Findings 84+85: escape LIKE metachars + drop whitespace-only input.
        term = f"%{escape_like(search.strip())}%"
        query = query.where(or_(
            Job.title.ilike(term, escape="\\"),
            Company.name.ilike(term, escape="\\"),
        ))

    # Count
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(Application.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(query)).all()

    items = []
    for app, job, co_name, resume_label, resume_filename in rows:
        items.append({
            "id": str(app.id),
            "job_id": str(app.job_id),
            "job_title": job.title,
            "company_name": co_name or "",
            "platform": job.platform,
            "job_url": job.url,
            "resume_id": str(app.resume_id),
            "resume_label": resume_label or resume_filename or "",
            "status": app.status,
            "apply_method": app.apply_method,
            "applied_at": app.applied_at.isoformat() if app.applied_at else None,
            "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
            "created_at": app.created_at.isoformat(),
            "notes": app.notes,
            # Feature C — expose provenance + top-level score on the list
            # view. Not including `applied_resume_text` here on purpose;
            # the text blob can be ~20KB and a 25-row list shouldn't ship
            # half a megabyte. Callers who want the full snapshot fetch
            # the single-application endpoint.
            "submission_source": app.submission_source,
            "applied_resume_score_overall": (
                (app.applied_resume_score_snapshot or {}).get("overall")
                if app.applied_resume_score_snapshot else None
            ),
        })

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/questions/{job_id}")
async def preview_job_questions(
    job_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview application questions for a job with pre-filled answers from answer book.

    Regression finding 182: this endpoint was returning an opaque
    HTTP 500 (plain text body, no JSON detail) for the Wiz SRE job
    across 4 consecutive calls while 3 other Greenhouse jobs
    returned 200. The symptom was reproducible but the root cause
    was hidden because (a) any raised exception here bubbled up to
    FastAPI's default 500 handler with no stack trace in logs, and
    (b) the inner `db.flush()` in `get_or_fetch_questions` caught and
    swallowed its own failure, leaving the session in an unpredictable
    state for the outer `db.commit()`.

    Defensive changes:
      1. Wrap the session-mutating section in try/except — on any
         error, rollback and return an HTTP 502 with a specific
         reason message so the on-call engineer can see what failed
         without having to hunt through traceback logs.
      2. Log exceptions with `exc_info=True` so the traceback makes
         it to the logging pipeline.
      3. The dedup/coercion in `get_or_fetch_questions` covers the
         most likely root causes (duplicate `field_key` INSERTs,
         NULL fields in cached rows).
    """
    import logging
    from app.services.question_service import get_or_fetch_questions, auto_populate_answer_book
    from app.workers.tasks._answer_prep import blocking_gaps, match_questions_to_answers
    from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, human_wall_for
    from app.services.submitters import auto_submittable_platforms

    logger = logging.getLogger(__name__)

    # Load job
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Find board slug
    board = (await db.execute(
        select(CompanyATSBoard).where(
            CompanyATSBoard.company_id == job.company_id,
            CompanyATSBoard.platform == job.platform,
            CompanyATSBoard.is_active.is_(True),
        )
    )).scalar_one_or_none()
    board_slug = board.slug if board else ""

    try:
        # Get or fetch questions (cached)
        ats_questions = await get_or_fetch_questions(db, job, board_slug)

        # Auto-populate answer book
        new_entries = await auto_populate_answer_book(db, user.id, ats_questions)
        await db.commit()

        # Load answer book entries
        ab_query = select(AnswerBookEntry).where(
            AnswerBookEntry.user_id == user.id,
            or_(
                AnswerBookEntry.resume_id.is_(None),
                AnswerBookEntry.resume_id == user.active_resume_id,
            ) if user.active_resume_id else AnswerBookEntry.resume_id.is_(None),
        )
        ab_result = await db.execute(ab_query)
        ab_entries = ab_result.scalars().all()

        # Merge (resume overrides base). Convert ORM objects to plain dicts so the
        # downstream matcher (which calls .get()) works correctly.
        merged: dict[str, dict] = {}
        for entry in ab_entries:
            key = entry.question_key
            if key not in merged or entry.resume_id is not None:
                merged[key] = {
                    "question_key": entry.question_key,
                    "answer": entry.answer or "",
                    "category": entry.category or "",
                    "source": entry.source or "base",
                }

        # Match questions to answers
        matched = match_questions_to_answers(ats_questions, list(merged.values()))
    except HTTPException:
        raise
    except Exception:
        # F182: don't let arbitrary exceptions surface as opaque 500s
        # with no body. Rollback any partial writes, log the trace,
        # and return a 502 (Bad Gateway / upstream fetch failed) with
        # a specific message so the client UI can show "couldn't
        # preview questions — try again" instead of a generic crash.
        await db.rollback()
        logger.exception(
            "Failed to preview questions for job_id=%s platform=%s board=%s",
            job_id, job.platform, board_slug,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not load application questions from the ATS provider. "
                "This usually means the job posting was removed or the "
                "ATS API is temporarily unavailable."
            ),
        )

    # Compute coverage
    total = len(matched)
    answered = sum(1 for m in matched if m.get("answer"))
    high_conf = sum(1 for m in matched if m.get("confidence") == "high" and m.get("answer"))

    # F346 — the apply gate. `blocking` lists required fields we won't
    # answer on the user's behalf; `schema` says whether the form we're
    # showing was extracted from the ATS or guessed from a template.
    # `safe_to_auto_submit` is the single boolean the apply path reads:
    # a guessed form is never safe to auto-submit, because a form we
    # invented cannot be a form we filled correctly.
    # F362 — the preview must agree with the gate. `apply_task` passes
    # satisfied_field_keys={"resume"} because it uploads the stored file
    # rather than typing it, so the Resume/CV alternative group is
    # satisfied there. Without the same signal here the UI listed
    # "Resume/CV" twice as blocking on an application the worker would
    # have accepted — the screen told the user to fix something that
    # wasn't broken.
    satisfied: set[str] = set()
    if user.active_resume_id:
        has_file = (await db.execute(
            select(Resume.id).where(
                Resume.id == user.active_resume_id,
                Resume.user_id == user.id,
                Resume.file_data.isnot(None),
            )
        )).scalar_one_or_none()
        if has_file:
            satisfied.add("resume")

    blocking = blocking_gaps(matched, satisfied_field_keys=satisfied)
    extraction_mode = (
        "fallback"
        if any(m.get("extraction_mode") == "fallback" for m in matched)
        else "extracted"
    )

    return {
        "questions": matched,
        "coverage": {
            "total": total,
            "answered": answered,
            "high_confidence": high_conf,
            "new_entries": new_entries,
        },
        "schema": {
            "extraction_mode": extraction_mode,
            "platform": job.platform,
            "supported": job.platform in SUPPORTED_QUESTION_PLATFORMS,
            # F368 — why a form we may even have read still needs a
            # person. None for platforms we can drive.
            "wall": human_wall_for(job.platform),
        },
        "blocking": blocking,
        # F368 — and the platform must be one we can actually drive. A
        # Lever form can be fully extracted and unblocked and still need
        # a person for its hCaptcha; saying "safe" there offered a Submit
        # the worker would refuse.
        "safe_to_auto_submit": (
            not blocking
            and extraction_mode == "extracted"
            and human_wall_for(job.platform) is None
            and job.platform in auto_submittable_platforms()
        ),
    }


@router.get("/{app_id}")
async def get_application(
    app_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single application detail."""
    result = await db.execute(
        select(Application)
        .options(joinedload(Application.job), joinedload(Application.resume))
        .where(Application.id == app_id, Application.user_id == user.id)
    )
    app = result.unique().scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    # Get company name
    co_name = ""
    if app.job and app.job.company_id:
        co = (await db.execute(select(Company.name).where(Company.id == app.job.company_id))).scalar_one_or_none()
        co_name = co or ""

    return {
        "id": str(app.id),
        "job": {
            "id": str(app.job.id),
            "title": app.job.title,
            "company_name": co_name,
            "platform": app.job.platform,
            "url": app.job.url,
        },
        "resume": {
            "id": str(app.resume.id),
            "label": app.resume.label or app.resume.filename,
        },
        "status": app.status,
        "apply_method": app.apply_method,
        "prepared_answers": app.prepared_answers,
        "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
        "applied_at": app.applied_at.isoformat() if app.applied_at else None,
        "platform_response": app.platform_response,
        "notes": app.notes,
        "created_at": app.created_at.isoformat(),
        # Feature C — apply-time snapshot. `applied_resume_text` can be
        # large (~20KB), so callers that just need the score / source
        # should read the list endpoint instead. Nullable for legacy
        # rows that predate the snapshot columns.
        "submission_source": app.submission_source,
        "applied_resume_text": app.applied_resume_text,
        "applied_resume_score_snapshot": app.applied_resume_score_snapshot,
        "ai_customization_log_id": (
            str(app.ai_customization_log_id) if app.ai_customization_log_id else None
        ),
    }


@router.patch("/{app_id}")
async def update_application(
    app_id: UUID,
    body: ApplicationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update application status/notes."""
    app = (await db.execute(
        select(Application).where(Application.id == app_id, Application.user_id == user.id)
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    # F194: `model_dump(exclude_unset=True)` gives us exactly the fields
    # the client sent, so the partial-update semantics of the old
    # `"status" in body` checks are preserved — setting a field to
    # null on purpose is distinguishable from omitting it.
    patch = body.model_dump(exclude_unset=True)

    if "status" in patch:
        new_status = patch["status"]
        allowed = VALID_TRANSITIONS.get(app.status, [])
        if new_status not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot transition from '{app.status}' to '{new_status}'. Allowed: {allowed}",
            )
        app.status = new_status
        if new_status == "applied" and not app.applied_at:
            app.applied_at = datetime.now(timezone.utc)
        elif new_status == "submitted" and not app.submitted_at:
            app.submitted_at = datetime.now(timezone.utc)

    if "notes" in patch:
        app.notes = patch["notes"]

    if "prepared_answers" in patch and app.status == "prepared":
        app.prepared_answers = patch["prepared_answers"]

    db.add(app)
    await db.commit()

    return {"id": str(app.id), "status": app.status, "notes": app.notes, "prepared_answers": app.prepared_answers}


@router.delete("/{app_id}")
async def withdraw_application(
    app_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Withdraw an application (soft-delete — data is preserved)."""
    app = (await db.execute(
        select(Application).where(Application.id == app_id, Application.user_id == user.id)
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    app.status = "withdrawn"
    await db.commit()
    return {"status": "withdrawn", "message": "Application withdrawn (data preserved)"}


# ═══════════════════════════════════════════════════════════════════
# Claude Routine Apply — submission confirmation + detail + promotion
# ═══════════════════════════════════════════════════════════════════

# Imports needed by the routine-apply endpoints below. Kept local to
# this block so the additions are easy to audit in isolation and the
# existing import section stays stable.
from fastapi import Request  # noqa: E402
from app.models.review import Review  # noqa: E402
from app.models.pipeline import PotentialClient  # noqa: E402
from app.models.application_submission import ApplicationSubmission  # noqa: E402
from app.models.routine_run import RoutineRun  # noqa: E402
from app.models.humanization_corpus import HumanizationCorpus  # noqa: E402
from app.schemas.routine import (  # noqa: E402
    ConfirmSubmittedRequest,
    ConfirmSubmittedResponse,
    PromoteAnswerRequest,
    PromoteAnswerResponse,
    SubmissionDetail,
)
# F261: ``log_action`` is now imported at the top of the file because
# the team-pipeline endpoints (added before this block) need it. The
# routine endpoints below also use it via the same module-level
# binding.


# Keys we refuse to store in payload_json under any circumstance. The
# routine should reject these fields on the browser side (abort the
# apply with status="pii_requested" before clicking Submit), but we
# double-check server-side in case a custom flow slips through.
# Case-insensitive substring match — "ssn_last_4", "applicant_dob",
# "dateOfBirth" all get caught.
_PII_BLOCKED_KEY_SUBSTRINGS: tuple[str, ...] = (
    "ssn", "social_security", "socialsecurity",
    "date_of_birth", "dateofbirth", "dob",
    "passport_number", "passportnumber",
)


def _payload_contains_pii(payload: dict) -> str | None:
    """Return the offending key name, or None. Case-insensitive."""
    for key in payload.keys():
        lower = key.lower()
        for banned in _PII_BLOCKED_KEY_SUBSTRINGS:
            if banned in lower:
                return key
    return None


def _levenshtein_crude(a: str, b: str) -> int:
    """Cheap edit-distance estimate for humanization_corpus capture.

    We avoid the full O(mn) DP here — humanization_corpus is opt-in
    analytics, not a correctness-critical computation, and the routine
    already computed the real distance on the routine side. When the
    routine sends an explicit `edit_distance`, we trust it. This
    helper is only a fallback when a caller forgets to compute.
    """
    if not a:
        return len(b or "")
    if not b:
        return len(a)
    # Use character-diff as a proxy — good enough for "is it a small
    # edit or a big rewrite" bucketing.
    return abs(len(a) - len(b)) + sum(1 for x, y in zip(a, b) if x != y)


@router.post("/{app_id}/confirm-submitted", response_model=ConfirmSubmittedResponse)
async def confirm_submitted(
    app_id: UUID,
    body: ConfirmSubmittedRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record a successful application submission from the Claude routine.

    Called by the routine after the browser-side submit confirms. The
    handler performs a platform-consistent update spanning:

    1. Write the ``application_submissions`` row (1:1 with the app).
    2. Flip ``Application.status`` to ``"applied"``, stamp
       ``applied_at``, set ``apply_method="claude_routine"`` and
       ``submission_source="routine"``. (Dry-run path skips this —
       see ``body.dry_run``.)
    3. Create an accepted ``Review`` row — matches the existing
       /reviews/apply pattern so the review event log is a single
       source of truth for "this job was applied to by this user."
    4. Create/advance the ``PotentialClient`` pipeline entry for the
       job's company so the pipeline card shows "1 application."
    5. Dispatch the same Celery feedback task that /reviews submit
       uses, writing company_boost / cluster_boost / geography_boost
       ScoringSignal rows that feed back into relevance scoring.
    6. Increment ``usage_count`` on every answer-book entry the
       routine referenced.
    7. Capture generated answers into ``humanization_corpus`` so the
       style-match pipeline has training data.
    8. Write an audit log row (``routine.application_submitted`` or
       ``routine.application_dry_run``).
    9. Increment the owning ``routine_runs.applications_submitted``
       counter (only on non-dry-run).

    Dry-run mode writes the submission row and the audit log only;
    everything else is skipped so the rest of the platform isn't
    told a live apply happened when it didn't.
    """
    # ── 1. Ownership + state checks ──────────────────────────────────
    app = (await db.execute(
        select(Application).where(
            Application.id == app_id,
            Application.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    # Idempotency — if a submission row already exists, 409 rather than
    # double-writing. The routine shouldn't retry confirm-submitted;
    # if it does (network blip, worker restart) we want the first
    # write to win and the second to be a visible error.
    existing = (await db.execute(
        select(ApplicationSubmission).where(
            ApplicationSubmission.application_id == app_id
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="A submission already exists for this application.",
        )

    # ── 2. PII firewall ──────────────────────────────────────────────
    pii_key = _payload_contains_pii(body.payload_json)
    if pii_key is not None:
        # 400 + explicit message so the routine can log it and abort.
        raise HTTPException(
            status_code=400,
            detail=(
                f"payload_json contains a PII-restricted field: {pii_key!r}. "
                "SSN, DOB, and passport numbers are never stored. "
                "Abort the apply and surface the field to the user."
            ),
        )

    # ── 3. Cross-reference answer-book IDs ───────────────────────────
    # For every answer with source in {manual_required, learned}, the
    # source_ref_id must be one of this user's answer-book entries.
    # This prevents a malicious client from linking a submission row
    # to another user's answers.
    referenced_ids = [
        a.source_ref_id for a in body.answers
        if a.source_ref_id is not None
    ]
    if referenced_ids:
        found = (await db.execute(
            select(AnswerBookEntry.id).where(
                AnswerBookEntry.id.in_(referenced_ids),
                AnswerBookEntry.user_id == user.id,
            )
        )).scalars().all()
        if set(found) != set(referenced_ids):
            raise HTTPException(
                status_code=400,
                detail="One or more answer source_ref_id values don't belong to this user.",
            )

    # ── 4. Routine-run ownership check ───────────────────────────────
    run: RoutineRun | None = None
    if body.routine_run_id is not None:
        run = (await db.execute(
            select(RoutineRun).where(
                RoutineRun.id == body.routine_run_id,
                RoutineRun.user_id == user.id,
            )
        )).scalar_one_or_none()
        if not run:
            raise HTTPException(
                status_code=400,
                detail="routine_run_id not found for this user.",
            )

    now = datetime.now(timezone.utc)

    # ── 5. Platform-sync (skipped when dry_run) ──────────────────────
    # Detected-issues aggregator. Core writes raise through; platform-
    # sync failures are logged to this list and returned to the caller
    # (the UI surfaces them in the submission detail tab).
    detected = list(body.detected_issues or [])
    pipeline_entry_id: UUID | None = None

    if not body.dry_run:
        # 5a. Status + metadata
        app.status = "applied"
        app.applied_at = now
        app.submitted_at = body.submitted_at
        app.apply_method = "claude_routine"
        app.submission_source = "routine"
        if body.routine_run_id:
            app.routine_run_id = body.routine_run_id

        # 5b. Accepted Review (matches /reviews/apply pattern).
        #
        # F344: this used to blind-INSERT, with a comment claiming
        # "reviews is an event log, not per-(job,user) state". That
        # stopped being true when F281 added
        # `UNIQUE INDEX uq_reviews_job_reviewer ON reviews (job_id,
        # reviewer_id)`. Any job the user had already reviewed — every
        # job they'd previously rejected, in particular — raised
        # IntegrityError here and the whole handler 500'd.
        #
        # That is the most damaging ordering possible: the ATS form has
        # ALREADY been submitted by the time we get here, so the
        # application really was sent and the platform recorded none of
        # it. Re-applying to a role you'd previously passed on is
        # completely routine, so this fired constantly.
        #
        # Upsert instead: applying is a stronger, later signal than any
        # earlier decision, so it wins.
        review = (await db.execute(
            select(Review).where(
                Review.job_id == app.job_id,
                Review.reviewer_id == user.id,
            )
        )).scalar_one_or_none()
        if review is None:
            review = Review(
                id=uuid.uuid4(),
                job_id=app.job_id,
                reviewer_id=user.id,
                decision="accepted",
                comment="Applied via Claude routine",
                tags=[],
            )
            db.add(review)
        else:
            prior = review.decision
            review.decision = "accepted"
            review.comment = (
                "Applied via Claude routine"
                + (f" (superseded earlier decision: {prior})" if prior != "accepted" else "")
            )

        # 5c. Flip Job.status (same side-effect as /reviews/apply).
        # Guarded: may be None in unusual test scenarios.
        job = (await db.execute(
            select(Job).where(Job.id == app.job_id)
        )).scalar_one_or_none()
        if job:
            # F344: "accepted" is one of the ACTIVE statuses covered by
            # F316's `uq_jobs_active_company_title`, so this flip can
            # collide with a twin row for the same (company, title).
            # A duplicate listing must not sink a submission that has
            # already left the browser — skip the flip and report it.
            from app.api.v1.jobs import _active_duplicates
            if (await _active_duplicates([job], "accepted", db)):
                detected.append(
                    "job_status_not_advanced: another active listing "
                    "exists for the same company and title"
                )
            else:
                job.status = "accepted"
            # 5d. Pipeline — create or advance.
            if job.company_id:
                client = (await db.execute(
                    select(PotentialClient).where(PotentialClient.company_id == job.company_id)
                )).scalar_one_or_none()
                if not client:
                    company = (await db.execute(
                        select(Company).where(Company.id == job.company_id)
                    )).scalar_one_or_none()
                    if company:
                        company.is_target = True
                        client = PotentialClient(
                            company_id=job.company_id,
                            stage="new_lead",
                            resume_id=app.resume_id,
                            applied_by=user.id,
                        )
                        db.add(client)
                        await db.flush()
                else:
                    client.resume_id = app.resume_id
                    client.applied_by = user.id
                if client:
                    pipeline_entry_id = client.id

        # 5e. Answer-book usage tracking.
        for a in body.answers:
            if a.source_ref_id is not None:
                entry = (await db.execute(
                    select(AnswerBookEntry).where(AnswerBookEntry.id == a.source_ref_id)
                )).scalar_one_or_none()
                if entry:
                    entry.usage_count = (entry.usage_count or 0) + 1

        # 5f. Run counter bump.
        if run is not None:
            run.applications_submitted = (run.applications_submitted or 0) + 1

    # ── 6. Persist the submission row (always; dry-run included) ─────
    # We redact payload values for PII-shaped fields even after the
    # firewall pass — identity fields that are safe to fill (email,
    # phone) get their values replaced with {type, len} metadata. The
    # firewall already rejected the hard-banned ones; this is a
    # belt-and-suspenders sanitization for log-friendly storage.
    sanitized_payload = _sanitize_payload_for_storage(body.payload_json)

    submission = ApplicationSubmission(
        id=uuid.uuid4(),
        application_id=app.id,
        routine_run_id=body.routine_run_id,
        submitted_at=body.submitted_at,
        job_url=body.job_url,
        ats_platform=body.ats_platform,
        form_fingerprint_hash=body.form_fingerprint_hash,
        payload_json=sanitized_payload,
        answers_json=[a.model_dump(mode="json") for a in body.answers],
        resume_version_hash=body.resume_version_hash,
        cover_letter_text=body.cover_letter_text,
        screenshot_keys=list(body.screenshot_keys),
        confirmation_text=body.confirmation_text,
        detected_issues=detected + (["dry_run"] if body.dry_run else []),
        profile_snapshot=dict(body.profile_snapshot),
    )
    db.add(submission)

    # ── 7. Humanization corpus capture for generated answers ─────────
    # Only when `draft_text` is supplied (routine opts in per-answer).
    if not body.dry_run:
        for a in body.answers:
            if a.source == "generated" and a.draft_text is not None:
                db.add(HumanizationCorpus(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    application_id=app.id,
                    question=a.question,
                    draft_text=a.draft_text,
                    final_text=a.answer,
                    edit_distance=(
                        a.edit_distance
                        if a.edit_distance > 0
                        else _levenshtein_crude(a.draft_text, a.answer)
                    ),
                ))

    try:
        await db.commit()
    except IntegrityError:
        # F344 backstop. The submission already went out to the ATS, so
        # a 500 here loses a real application. Surface a specific 409
        # the caller can act on rather than a bare Internal Server Error.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "The application was submitted to the ATS but could not be "
                "recorded: it conflicts with an existing row for this job. "
                "Re-check the job's review and application state."
            ),
        )
    await db.refresh(submission)

    # ── 8. Post-commit hooks (feedback task + audit) ─────────────────
    # Dispatched AFTER commit so a failing Celery broker doesn't roll
    # back the submission. Mirrors the pattern in reviews.submit_review.
    if not body.dry_run:
        try:
            # Find the review we just created so the feedback task can
            # read it. Look up by (job_id, reviewer_id, created_at desc).
            fresh_review = (await db.execute(
                select(Review).where(
                    Review.job_id == app.job_id,
                    Review.reviewer_id == user.id,
                ).order_by(Review.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            if fresh_review:
                from app.workers.tasks.feedback_task import process_review_feedback_task
                process_review_feedback_task.delay(str(fresh_review.id))
        except Exception as e:
            # Feedback-dispatch failure is non-fatal — submission is
            # already written. Surface in detected_issues for the UI.
            detected.append(f"feedback_dispatch_failed: {type(e).__name__}")

    # Audit log — fail-open (the helper swallows its own exceptions).
    await log_action(
        db, user,
        action=("routine.application_dry_run" if body.dry_run else "routine.application_submitted"),
        resource="application",
        request=request,
        metadata={
            "application_id": str(app.id),
            "submission_id": str(submission.id),
            "job_id": str(app.job_id),
            "platform": body.ats_platform,
            "routine_run_id": str(body.routine_run_id) if body.routine_run_id else None,
            "answer_count": len(body.answers),
            "dry_run": body.dry_run,
        },
    )

    return ConfirmSubmittedResponse(
        application_id=app.id,
        submission_id=submission.id,
        pipeline_entry_id=pipeline_entry_id,
        dry_run=body.dry_run,
        detected_issues=submission.detected_issues,
    )


# Payload sanitization — runs on every submission write. PII-shaped
# fields get their values replaced with a type+length stub so a later
# audit or UI view can tell "a phone was entered, length 12" without
# storing the actual number. Identity-class fields that are safe to
# store (salary, notice period, cover letter) pass through untouched.
_SANITIZE_KEY_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("email", "email"),
    ("phone", "phone"),
    ("mobile", "phone"),
    ("zip", "postal"),
    ("postal", "postal"),
)


def _sanitize_payload_for_storage(payload: dict) -> dict:
    out: dict = {}
    for k, v in payload.items():
        lower = k.lower()
        matched_type: str | None = None
        for substr, field_type in _SANITIZE_KEY_SUBSTRINGS:
            if substr in lower:
                matched_type = field_type
                break
        if matched_type is not None and isinstance(v, str):
            out[k] = {"type": matched_type, "len": len(v)}
        else:
            out[k] = v
    return out


@router.get("/{app_id}/submission", response_model=SubmissionDetail)
async def get_application_submission(
    app_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the stored submission detail for an application.

    Renders the "Submission Detail" tab on the Application detail
    page. 404 when the application has no submission (manual apps
    filed pre-routine, review-queue apps).
    """
    # Join via application ownership so we enforce user scoping in
    # one query instead of two round-trips.
    row = (await db.execute(
        select(ApplicationSubmission).join(
            Application, Application.id == ApplicationSubmission.application_id
        ).where(
            ApplicationSubmission.application_id == app_id,
            Application.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="No submission recorded for this application.")

    return SubmissionDetail(
        id=row.id,
        application_id=row.application_id,
        routine_run_id=row.routine_run_id,
        submitted_at=row.submitted_at,
        job_url=row.job_url,
        ats_platform=row.ats_platform,
        form_fingerprint_hash=row.form_fingerprint_hash,
        payload_json=row.payload_json or {},
        answers_json=row.answers_json or [],
        resume_version_hash=row.resume_version_hash,
        cover_letter_text=row.cover_letter_text,
        screenshot_keys=row.screenshot_keys or [],
        confirmation_text=row.confirmation_text,
        detected_issues=row.detected_issues or [],
        profile_snapshot=row.profile_snapshot or {},
        created_at=row.created_at,
    )


class SubmitApplicationRequest(BaseModel):
    """Trigger server-side submission of a prepared application.

    ``dry_run`` fills every field in the real form and stops immediately
    before the submit click. It is the only safe way to exercise an
    adapter against a live posting, and it does NOT move the application
    to ``submitted`` — a dry run proves the form can be filled, it did
    not apply to anything.
    """

    dry_run: bool = False
    model_config = ConfigDict(extra="forbid")


class SubmitApplicationResponse(BaseModel):
    task_id: str
    status: str
    application_id: str
    dry_run: bool


@router.post("/{app_id}/submit", response_model=SubmitApplicationResponse)
async def submit_application(
    app_id: UUID,
    body: SubmitApplicationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Queue an application for unattended submission (F347).

    This endpoint only *enqueues*. Every gate — extracted-not-guessed
    schema, adapter exists, all required fields confidently resolved,
    no CAPTCHA/login wall — runs inside the task, because the answers
    and the form must be re-read at submit time rather than trusted from
    whenever the user last previewed them. An application that fails a
    gate comes back as ``needs_user`` with a per-field reason, not as an
    error here.
    """
    app_row = (await db.execute(
        select(Application).where(
            Application.id == app_id,
            Application.user_id == user.id,
        )
    )).scalar_one_or_none()
    if app_row is None:
        raise HTTPException(status_code=404, detail="Application not found")

    # Re-submitting something already sent would create a duplicate
    # application at the employer, which we cannot undo.
    if app_row.status in ("submitted", "applied", "interview", "offer"):
        raise HTTPException(
            status_code=409,
            detail=f"Application is already {app_row.status} — refusing to submit it again",
        )
    if app_row.status == "in_flight":
        raise HTTPException(
            status_code=409,
            detail="Submission already in progress for this application",
        )

    from app.workers.tasks.apply_task import submit_application_task

    task = submit_application_task.delay(str(app_id), dry_run=body.dry_run)
    return SubmitApplicationResponse(
        task_id=task.id,
        status="queued",
        application_id=str(app_id),
        dry_run=body.dry_run,
    )


@router.post("/{app_id}/promote-answer", response_model=PromoteAnswerResponse)
async def promote_answer(
    app_id: UUID,
    body: PromoteAnswerRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Promote a generated answer from a submission into the answer book.

    User clicks "Save to Answer Book" next to a `generated` answer
    in the submission detail tab. We create (or find) an answer-book
    entry with ``source="learned"``, ``is_locked=False`` — so future
    routine runs prefer it over LLM generation, and the user can still
    edit the question text if they want to canonicalize the phrasing.

    Idempotent: if the exact ``question_key`` already exists for this
    user as a base entry, we update the answer and return
    ``already_existed=True``. Useful when the user clicks promote on
    two similar-but-different questions that normalize to the same key.
    """
    # Ownership check (fail early with a clean 404).
    app = (await db.execute(
        select(Application).where(
            Application.id == app_id,
            Application.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    # Inline the normalizer to avoid a cross-router import.
    from app.api.v1.answer_book import normalize_question_key
    question_key = normalize_question_key(body.question)

    # Don't let promote walk around the lock — if the key collides
    # with a routine-required one, reject.
    from app.services.answer_book_seed import REQUIRED_QUESTION_KEYS
    if question_key in REQUIRED_QUESTION_KEYS:
        raise HTTPException(
            status_code=400,
            detail="This question overlaps a routine-required entry and cannot be promoted.",
        )

    existing = (await db.execute(
        select(AnswerBookEntry).where(
            AnswerBookEntry.user_id == user.id,
            AnswerBookEntry.resume_id.is_(None),
            AnswerBookEntry.question_key == question_key,
        )
    )).scalar_one_or_none()

    if existing:
        # Don't overwrite if the existing answer is manual (user-typed)
        # — they may have carefully phrased it. Only update when the
        # source is already `learned` or equivalent.
        already = True
        if existing.source in ("learned", "generated"):
            existing.answer = body.answer
        entry_id = existing.id
    else:
        already = False
        new_entry = AnswerBookEntry(
            id=uuid.uuid4(),
            user_id=user.id,
            resume_id=None,
            category="custom",
            question=body.question,
            question_key=question_key,
            answer=body.answer,
            source="learned",
            is_locked=False,
        )
        db.add(new_entry)
        await db.flush()
        entry_id = new_entry.id

    # Flip promoted flag on any matching humanization_corpus row so
    # the style-match loader can deprioritize it.
    corpus_rows = (await db.execute(
        select(HumanizationCorpus).where(
            HumanizationCorpus.user_id == user.id,
            HumanizationCorpus.application_id == app_id,
            HumanizationCorpus.question == body.question,
        )
    )).scalars().all()
    for row in corpus_rows:
        row.promoted_to_answer_book = True

    await db.commit()

    return PromoteAnswerResponse(
        answer_book_entry_id=entry_id,
        already_existed=already,
    )
