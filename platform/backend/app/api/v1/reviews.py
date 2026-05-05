"""Review workflow API endpoints."""

from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.job import Job
from app.models.review import Review
from app.models.company import Company
from app.models.pipeline import PotentialClient
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.schemas.review import ReviewCreate, ReviewOut
from app.utils.audit import log_action

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("", response_model=ReviewOut)
async def submit_review(
    body: ReviewCreate,
    request: Request,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    # Validate job exists. F238: eager-load `description` so the
    # training-data capture below can read text_content without a
    # lazy-load (async sessions don't auto-resolve relationships).
    result = await db.execute(
        select(Job)
        .options(joinedload(Job.description))
        .where(Job.id == body.job_id)
    )
    job = result.unique().scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Normalize decision: frontend sends accept/reject/skip, store as accepted/rejected/skipped
    decision_map = {"accept": "accepted", "reject": "rejected", "skip": "skipped"}
    normalized = decision_map.get(body.decision, body.decision)

    # Regression finding 73: rejection tags must only be persisted on
    # `decision="rejected"` rows. The frontend carried selectedTags across
    # prev/next navigation (finding 72) AND submitted them in the Accept
    # payload too — so a reviewer who armed tags for job A, then clicked
    # Next and hit Accept on job B, produced an accepted-review row with
    # rejection tags attached. Downstream rejection-reason histograms
    # double-count those tags because they show up on both accepted and
    # rejected rows. Silent-drop here (rather than 400) because the
    # reviewer's intent on Accept is "this is good" — surfacing an error
    # they never triggered would be a worse UX. The frontend fix
    # (tester-owned) sets tags=[] on accept/skip too; this is
    # defense-in-depth for hand-crafted POSTs or a future frontend regression.
    persisted_tags = list(body.tags) if normalized == "rejected" else []

    # Create review
    review = Review(
        job_id=body.job_id,
        reviewer_id=user.id,
        decision=normalized,
        comment=body.comment,
        tags=persisted_tags,
    )
    db.add(review)

    # Update job status
    if normalized in ("accepted", "rejected"):
        job.status = normalized
    elif normalized == "skipped":
        job.status = "under_review"

    # If accepted, create/update pipeline entry
    if normalized == "accepted":
        result = await db.execute(
            select(PotentialClient).where(PotentialClient.company_id == job.company_id)
        )
        client = result.scalar_one_or_none()
        if not client:
            # Auto-create pipeline entry
            result = await db.execute(select(Company).where(Company.id == job.company_id))
            company = result.scalar_one_or_none()
            if company:
                company.is_target = True
                client = PotentialClient(
                    company_id=job.company_id,
                    stage="new_lead",
                )
                db.add(client)
        # Regression finding 192: deliberately NO `accepted_jobs_count += 1`
        # here. The column was incremented on every accept event but
        # never decremented on reject/flip, so it drifted from reality
        # (a single job flipped accept→reject→accept contributed +2).
        # `/pipeline` listing and detail now compute the count live via
        # `SELECT COUNT(*) FROM jobs WHERE status='accepted' AND company_id=?`
        # so the stored column is no longer read for display. Left as a
        # legacy column (drop requires a migration + downstream export
        # sweep) but stopped writing to it so future data doesn't drift
        # further.

    # F281 (closes F156) — surface the UNIQUE(job_id, reviewer_id)
    # constraint as a clean 409 instead of leaking the underlying
    # IntegrityError as a 500. The handler does no pre-check: a
    # check-then-write would still race because two parallel
    # requests can both pass the check before either commits. The
    # DB constraint is the only race-safe gate; we just translate
    # its violation to a useful client error here. ``rollback()`` is
    # required so the session is reusable for the audit log /
    # response path that follows. Note: the constraint is enforced
    # by ``uq_reviews_job_reviewer`` (migration ``k7l8m9n0o1p2``);
    # falling back to a substring-on-error-message check would be
    # fragile, so we test for the unique-violation specifically.
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Heuristic: any ``UniqueViolation`` whose constraint matches
        # the reviews-pair constraint name is the F156 race. Other
        # IntegrityErrors (FK to a deleted Job, etc.) re-raise so the
        # original 500 surfaces with full traceback for ops debugging.
        constraint = getattr(getattr(exc, "orig", None), "diag", None)
        constraint_name = getattr(constraint, "constraint_name", "") if constraint else ""
        if "uq_reviews_job_reviewer" in (constraint_name or "") or "uq_reviews_job_reviewer" in str(exc).lower():
            raise HTTPException(
                status_code=409,
                detail=(
                    "You've already reviewed this job. To change your "
                    "decision, edit or delete the existing review first."
                ),
            )
        raise

    await db.refresh(review)

    # Regression finding 113: audit trail for review actions
    await log_action(
        db, user,
        action=f"review.{normalized}",
        resource="review",
        request=request,
        metadata={"job_id": str(body.job_id), "review_id": str(review.id)},
    )

    # Dispatch feedback processing
    from app.workers.tasks.feedback_task import process_review_feedback_task
    process_review_feedback_task.delay(str(review.id))

    # F238: training-data capture. One row per review event with
    # (resume_text, JD, decision) — the cleanest labeled signal we
    # have for a future "is this resume a good match for this job"
    # model. Side-effect-only: if the capture fails, the review
    # write still goes through (the helper logs + swallows). PII
    # scrubbing on resume + JD is handled by the helper.
    try:
        from app.utils.training_capture import capture_resume_match
        from app.models.resume import Resume
        # Pull the reviewer's active resume text. If they have no
        # active resume (admin spot-checking, freshly-archived
        # resume), skip the capture — there's no labeled-input pair
        # to record without it.
        if user.active_resume_id:
            r_row = (await db.execute(
                select(Resume).where(Resume.id == user.active_resume_id)
            )).scalar_one_or_none()
            if r_row and r_row.text_content:
                jd_text = ""
                if job.description and job.description.text_content:
                    jd_text = job.description.text_content
                await capture_resume_match(
                    db,
                    user_id=user.id,
                    resume_text=r_row.text_content,
                    job_title=job.title,
                    job_description=jd_text,
                    decision=normalized,
                    job_id=job.id,
                    role_cluster=job.role_cluster,
                )
    except Exception:
        # Side-effect capture must NOT break the review write.
        # The helper itself swallows + logs, but defense-in-depth
        # against import errors / unexpected attribute misses.
        pass

    out = ReviewOut.model_validate(review)
    out.reviewer_name = user.name
    return out


@router.get("")
async def list_reviews(
    job_id: UUID | None = None,
    # Regression finding 195: `decision: str | None` silently accepted
    # typos (`decision=bogus` → empty list, indistinguishable from "no
    # matching rows"). Same F128 pattern chased through 8+ endpoints in
    # earlier rounds (F162/F179/F187/F191). The DB column only ever
    # stores the three values below (see `reviews.py:37` decision_map
    # and `Review.decision` enum), so Literal-validate at the route
    # boundary and let FastAPI 422 the unknown variants. Note: the
    # stored form is `accepted`/`rejected`/`skipped` (past tense) — not
    # the `accept`/`reject`/`skip` wire form the POST endpoint accepts
    # via `decision_map`. We validate on the stored form here because
    # that's what callers are filtering against.
    decision: Literal["accepted", "rejected", "skipped"] | None = None,
    page: int = Query(1, ge=1),
    # F319 (closes F108 query-param drift): accept BOTH legacy
    # ``per_page`` and canonical ``page_size``. ``per_page`` is
    # honoured when explicitly supplied (back-compat — existing
    # clients keep working). New callers should use ``page_size``
    # to match every other paginated endpoint in the API.
    per_page: int | None = Query(default=None, ge=1, le=200, deprecated=True),
    page_size: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(Review).options(joinedload(Review.reviewer))

    # F319: legacy ``per_page`` wins when explicitly supplied;
    # otherwise we use the canonical ``page_size``.
    per_page = per_page if per_page is not None else page_size

    if job_id:
        query = query.where(Review.job_id == job_id)
    if decision:
        query = query.where(Review.decision == decision)

    query = query.order_by(Review.created_at.desc())

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    reviews = result.unique().scalars().all()

    items = []
    for r in reviews:
        item = ReviewOut.model_validate(r)
        item.reviewer_name = r.reviewer.name if r.reviewer else None
        items.append(item)

    return {"items": items, "total": total, "page": page, "page_size": per_page, "total_pages": (total + per_page - 1) // per_page}


# --- Feature C: "Applied" action from the review queue -----------------------
#
# POST /reviews/apply — a reviewer hits the fourth review-queue button
# ("Applied", P shortcut) to mark a job as actually submitted, not just
# "worth pursuing". Applied implies Accept, so the handler atomically:
#   1. Creates a Review row with decision="accepted" (so analytics and
#      /jobs/review-queue exclusion treat it identically to a plain Accept).
#   2. Flips Job.status="accepted".
#   3. Creates/updates the company's PotentialClient pipeline row.
#   4. Upserts the user's Application row for this job, with
#      status="applied", applied_at=now, and the three snapshot columns
#      populated so the Applications page can show "what we sent".
#
# The state machine in applications.py VALID_TRANSITIONS doesn't list a
# direct `None -> applied` transition — that's intentional for PATCH
# /applications/{id}, which represents a user editing an existing app.
# This endpoint bypasses the state machine because it's a CREATE path:
# there's no "current state" to transition from.

from datetime import datetime as _dt, timezone as _tz
from pydantic import BaseModel as _ABase, ConfigDict as _AConfig, Field as _AField  # noqa: E402
from app.models.application import Application
from app.models.resume import Resume, ResumeScore, AICustomizationLog


class ApplyFromReviewRequest(_ABase):
    """Payload for POST /reviews/apply.

    Most fields are optional — the reviewer may just click Applied with
    no customization, in which case we snapshot the raw active resume
    text + the most recent ResumeScore for this (resume, job) pair.
    """
    model_config = _AConfig(extra="forbid")

    job_id: UUID
    # Optional — defaults to the reviewer's `user.active_resume_id`.
    # Allows the UI to let the user pick a different resume before
    # clicking Applied (not shipped in the first frontend pass but
    # unblocks the next iteration).
    resume_id: UUID | None = None
    # F129 precedent — 5000-char cap on free-text user input.
    notes: str | None = _AField(default=None, max_length=5000)
    # When present, overrides the raw resume text at snapshot time —
    # typically the Claude-customized output from the AI customize flow.
    # Cap at 100k chars which is ~5x a long resume (PDFs clock in at
    # 10-20k typically); matches the upper bound on the customize API.
    customized_resume_text: str | None = _AField(default=None, max_length=100_000)
    # If an AI run produced ``customized_resume_text``, pass its log id
    # for audit linkage. The FK is validated below.
    ai_customization_log_id: UUID | None = None


@router.post("/apply")
async def apply_from_review(
    body: ApplyFromReviewRequest,
    request: Request,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    """Mark a job as Applied from the review queue.

    Semantics: Applied implies Accept. The handler flips ``Job.status``
    to ``accepted``, creates the accepted Review row and the company
    pipeline entry (same side-effects as submit_review with Accept),
    and upserts the user's Application row with the apply-time snapshot.
    """
    # 1. Load the job. 404 if missing.
    job = (await db.execute(select(Job).where(Job.id == body.job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # 2. Resolve the resume to snapshot. Caller override wins; otherwise
    #    fall back to the reviewer's active resume.
    resume_id = body.resume_id or user.active_resume_id
    if not resume_id:
        raise HTTPException(
            status_code=400,
            detail="No resume selected. Set an active resume or pass resume_id.",
        )
    resume = (await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )).scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    # 3. If the caller claims an AI customization log, validate it
    #    belongs to this user + matches (resume, job). Don't let a
    #    client link an Application to someone else's AI log.
    ai_log_id = None
    if body.ai_customization_log_id:
        ai_log = (await db.execute(
            select(AICustomizationLog).where(
                AICustomizationLog.id == body.ai_customization_log_id,
                AICustomizationLog.user_id == user.id,
                AICustomizationLog.resume_id == resume.id,
                AICustomizationLog.job_id == job.id,
            )
        )).scalar_one_or_none()
        if not ai_log:
            raise HTTPException(
                status_code=400,
                detail="ai_customization_log_id not found for this user/resume/job.",
            )
        ai_log_id = ai_log.id

    # 4. Snapshot the resume score at apply-time. Uses the same
    #    "most recent" ordering as apply-readiness (resume_scores has
    #    no UNIQUE constraint in legacy rows) so duplicates don't
    #    raise MultipleResultsFound.
    score_row = (await db.execute(
        select(ResumeScore)
        .where(ResumeScore.resume_id == resume.id, ResumeScore.job_id == job.id)
        .order_by(ResumeScore.scored_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    score_snapshot = None
    if score_row is not None:
        score_snapshot = {
            "overall": round(score_row.overall_score, 1),
            "keyword": round(score_row.keyword_score, 1),
            "role_match": round(score_row.role_match_score, 1),
            "format": round(score_row.format_score, 1),
        }

    # 5. Resume text to snapshot. Customized text wins; otherwise raw
    #    resume body. If neither is available (legacy resume with no
    #    extracted text), store an empty string — we still want the
    #    other snapshot columns populated.
    snapshot_text = body.customized_resume_text or resume.text_content or ""

    now = _dt.now(_tz.utc)

    # 6. Upsert the Application. The UNIQUE(user_id, job_id) constraint
    #    means a second Applied click flips the existing row to a
    #    second-submission state rather than duplicating. We treat that
    #    as "user re-applied" — refresh the snapshot and applied_at but
    #    keep the row.
    existing_app = (await db.execute(
        select(Application).where(
            Application.user_id == user.id,
            Application.job_id == job.id,
        )
    )).scalar_one_or_none()

    if existing_app:
        existing_app.status = "applied"
        existing_app.applied_at = now
        existing_app.resume_id = resume.id
        existing_app.applied_resume_text = snapshot_text
        existing_app.applied_resume_score_snapshot = score_snapshot
        existing_app.ai_customization_log_id = ai_log_id
        existing_app.submission_source = "review_queue"
        if body.notes is not None:
            existing_app.notes = body.notes
        app_id = existing_app.id
        app_is_new = False
    else:
        new_app = Application(
            user_id=user.id,
            job_id=job.id,
            # F261: denormalise company_id at apply-time so the team
            # feed can filter/group by company without an Application⨝
            # Job join. job.company_id is the source of truth; the
            # value never changes for an existing application.
            company_id=job.company_id,
            resume_id=resume.id,
            status="applied",
            apply_method="manual_copy",
            prepared_answers=[],
            applied_at=now,
            notes=body.notes or "",
            applied_resume_text=snapshot_text,
            applied_resume_score_snapshot=score_snapshot,
            ai_customization_log_id=ai_log_id,
            submission_source="review_queue",
        )
        db.add(new_app)
        await db.flush()
        app_id = new_app.id
        app_is_new = True

    # 7. Record the accepted Review. Applied implies Accept. We always
    #    write a fresh Review row rather than updating prior ones — the
    #    reviews table is an event log, not a per-(job,reviewer) state.
    review = Review(
        job_id=job.id,
        reviewer_id=user.id,
        decision="accepted",
        comment=(body.notes or "Applied via review queue"),
        tags=[],
    )
    db.add(review)

    # 8. Flip Job.status and create/refresh the PotentialClient pipeline
    #    entry — mirrors the side-effects in submit_review for Accept.
    job.status = "accepted"

    client = (await db.execute(
        select(PotentialClient).where(PotentialClient.company_id == job.company_id)
    )).scalar_one_or_none()
    if not client:
        company = (await db.execute(
            select(Company).where(Company.id == job.company_id)
        )).scalar_one_or_none()
        if company:
            company.is_target = True
            db.add(PotentialClient(
                company_id=job.company_id,
                stage="new_lead",
                resume_id=resume.id,
                applied_by=user.id,
            ))
    else:
        # Annotate the existing pipeline row with who applied + which
        # resume was used. `resume_id` / `applied_by` on PotentialClient
        # were explicitly added for this kind of link — we stamp them
        # here on every Applied click, overwriting any prior value
        # because the most recent application is the most relevant
        # signal for Pipeline views.
        client.resume_id = resume.id
        client.applied_by = user.id

    await db.commit()

    # 9. Audit. Dedicated action verb so the audit-log filter UI can
    #    show "who Applied to what" distinctly from plain Accepts.
    await log_action(
        db, user,
        action="review.applied",
        resource="application",
        request=request,
        metadata={
            "job_id": str(job.id),
            "application_id": str(app_id),
            "resume_id": str(resume.id),
            "application_is_new": app_is_new,
            "ai_customization_log_id": str(ai_log_id) if ai_log_id else None,
        },
    )

    return {
        "application_id": str(app_id),
        "job_id": str(job.id),
        "status": "applied",
        "application_is_new": app_is_new,
    }
