"""Resume upload, ATS scoring, and AI customization endpoints."""

import uuid
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, undefer

from app.database import get_db
from app.models.resume import Resume, ResumeScore
from app.models.job import Job, JobDescription
from app.models.user import User
from app.models.role_config import RoleClusterConfig
from app.api.deps import get_current_user
from app.schemas.resume import CustomizeRequest, ResumeLabelUpdate
from app.workers.tasks._resume_parser import extract_text
from app.workers.tasks._ats_scoring import compute_ats_score
from app.utils.sql import escape_like


async def _get_relevant_clusters(db: AsyncSession) -> list[str]:
    """Get role clusters marked as relevant."""
    result = await db.execute(
        select(RoleClusterConfig.name).where(
            RoleClusterConfig.is_relevant == True,
            RoleClusterConfig.is_active == True,
        )
    )
    clusters = result.scalars().all()
    return list(clusters) if clusters else ["infra", "security"]

router = APIRouter(prefix="/resume", tags=["resume"])

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
MIN_FILE_SIZE = 256  # bytes — anything smaller can't be a real resume
ALLOWED_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}

# Minimum number of words we need to pull out of a file to call it a usable
# resume. Below this we assume extraction failed (scanned PDF, corrupt DOCX,
# plain-text-renamed-to-.pdf) and reject the upload outright rather than
# persisting a broken row with status="error".
MIN_WORD_COUNT = 50


# F132: `POST /resume/upload` receives `label` as a query param (matches
# the current frontend in `lib/api.ts::uploadResume` which builds
# `?label=<encoded>`). Pre-fix the param was typed `label: str = ""`
# with no cap, so any value >100 chars crashed with a 500 at the DB
# insert (`Resume.label` is `String(100)`). Cap here so FastAPI 422s
# the 500-char label at parse time. Kept as `Query(...)` rather than
# promoting to `Form(...)` — the frontend doesn't send multipart for
# this field, and flipping would be a breaking change the UI would
# have to match in the same release. If multipart-form label support
# becomes a requirement, add a second `Form(...)` param and fall back
# through both; don't migrate the existing query-param in place.
_LABEL_MAX_LEN = 100


@router.post("/upload")
async def upload_resume(
    file: UploadFile = File(...),
    label: str = Query(default="", max_length=_LABEL_MAX_LEN),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a resume (PDF or DOCX) and extract text."""
    # Validate file type
    content_type = file.content_type or ""
    file_type = ALLOWED_TYPES.get(content_type)
    if not file_type:
        # Try extension fallback
        filename = file.filename or ""
        if filename.lower().endswith(".pdf"):
            file_type = "pdf"
        elif filename.lower().endswith(".docx"):
            file_type = "docx"
        else:
            raise HTTPException(status_code=400, detail="Only PDF and DOCX files are accepted")

    # Read file
    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(file_bytes) < MIN_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail="File is too small to be a valid resume",
        )
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds 5MB limit")

    # Magic-byte check so a plain .txt renamed to .pdf (or a random binary
    # with a .pdf extension) gets rejected here instead of silently failing
    # text extraction downstream and leaving behind a status="error" row.
    header = file_bytes[:4]
    if file_type == "pdf" and not file_bytes[:5] == b"%PDF-":
        raise HTTPException(
            status_code=400,
            detail="File is not a valid PDF (missing %PDF header)",
        )
    if file_type == "docx" and header != b"PK\x03\x04":
        # DOCX is a ZIP package; real DOCX files start with the PK ZIP header.
        raise HTTPException(
            status_code=400,
            detail="File is not a valid DOCX (missing ZIP header)",
        )

    # Extract text
    text_content = extract_text(file_bytes, file_type)
    word_count = len(text_content.split()) if text_content else 0

    # Reject at the API boundary instead of persisting a broken row. Prior
    # behaviour (status="error" rows in the DB) caused DB clutter and
    # misleading UX — the user sees an upload succeed and then wonders why
    # scoring never runs.
    if word_count < MIN_WORD_COUNT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not extract readable text from the file "
                f"(got {word_count} words, need at least {MIN_WORD_COUNT}). "
                f"Please upload a text-based (not scanned) PDF or DOCX."
            ),
        )

    resume = Resume(
        id=uuid.uuid4(),
        user_id=user.id,
        label=label or (file.filename or "resume").rsplit(".", 1)[0],
        filename=file.filename or "resume",
        file_type=file_type,
        text_content=text_content,
        word_count=word_count,
        status="ready",
        # Persist the original bytes so the Resume Score page can render
        # an inline preview (PDF iframe / DOCX download). Pre-b8c9d0e1f2g3
        # the bytes were thrown away after extraction; new uploads keep
        # them. The 5 MB upload cap above bounds the column size.
        file_data=file_bytes,
    )
    db.add(resume)
    await db.flush()  # flush to DB so FK constraint is satisfied

    # Auto-set as active if this is the user's first resume
    if not user.active_resume_id:
        user.active_resume_id = resume.id
        db.add(user)

    # Auto-populate the answer book from the resume's extracted text.
    # Previously users had to click an "Import from Resume" button on the
    # Answer Book page after uploading — a redundant step since the data
    # already exists in `text_content` at this point. The helper is
    # idempotent (checks for existing keys before inserting), so a user
    # with multiple resumes uploading a second one just fills in any
    # fields the first resume didn't have. Failures here don't block the
    # upload — if the extractor regex hits a weird edge case, we'd rather
    # the user still get a ready resume than see their upload 500.
    try:
        from app.api.v1.answer_book import auto_populate_from_resume
        await auto_populate_from_resume(db, user.id, resume)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "auto_populate_from_resume failed for resume_id=%s",
            resume.id,
            exc_info=True,
        )

    await db.commit()
    await db.refresh(resume)

    # Regression finding 96: kick off scoring automatically on upload.
    # Before this hook, `status=ready` was the ONLY signal we gave back
    # and the user had to find and click the manual "Rescore" button on
    # the Resume Score page before any `ResumeScore` rows existed. For
    # the 11 days prior to this fix, that meant a brand-new upload
    # showed `jobs_scored=0` until someone noticed. Fire-and-forget: the
    # task has its own transaction + delete-and-replace semantics, so a
    # failed dispatch (redis down, worker offline) still leaves the
    # `Resume` row valid and the nightly beat schedule
    # (`rescore_all_active_resumes`) will catch up.
    try:
        from app.workers.tasks.resume_score_task import score_resume_task
        score_resume_task.delay(str(resume.id))
    except Exception:
        # Deliberately swallowed: upload succeeded, scoring is a
        # best-effort kicker. The nightly beat catches stragglers.
        pass

    return {
        "id": str(resume.id),
        "label": resume.label,
        "filename": resume.filename,
        "file_type": resume.file_type,
        "word_count": resume.word_count,
        "status": resume.status,
        "uploaded_at": resume.uploaded_at.isoformat(),
        "text_preview": text_content[:500] if text_content else "",
        "is_active": str(user.active_resume_id) == str(resume.id),
        # Always True for fresh uploads — the bytes were just persisted.
        # Surfaced so the frontend can render the Preview button without
        # a second round-trip to /resume to re-fetch the list.
        "has_file_data": True,
    }


@router.post("/switch/{resume_id}")
async def switch_active_resume(
    resume_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Switch the user's active resume/persona."""
    result = await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )
    resume = result.scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    user.active_resume_id = resume.id
    db.add(user)

    # Backfill answer-book fields from the newly-active resume. A user
    # with multiple resumes may have uploaded one with an email and a
    # different one with a LinkedIn URL — switching pulls whichever
    # fields the new one has that the old one didn't. Idempotent by
    # question_key, so switching back-and-forth never creates dupes.
    # Errors here don't block the switch — the worst case is the new
    # resume's personal-info fields just don't auto-populate.
    try:
        from app.api.v1.answer_book import auto_populate_from_resume
        await auto_populate_from_resume(db, user.id, resume)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "auto_populate_from_resume failed on switch for resume_id=%s",
            resume.id,
            exc_info=True,
        )

    await db.commit()

    return {
        "active_resume_id": str(resume.id),
        "label": resume.label,
        "message": f"Switched to '{resume.label or resume.filename}'",
    }


@router.post("/clear-active")
async def clear_active_resume(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear the user's active resume (exit persona mode)."""
    user.active_resume_id = None
    db.add(user)
    await db.commit()
    return {"active_resume_id": None, "message": "Active resume cleared"}


@router.get("/active")
async def get_active_resume(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the user's active resume details with score summary."""
    if not user.active_resume_id:
        return {"active_resume": None}

    result = await db.execute(
        select(Resume).where(Resume.id == user.active_resume_id, Resume.user_id == user.id)
    )
    resume = result.scalar_one_or_none()
    if not resume:
        return {"active_resume": None}

    # Score summary for this resume — F96 extended query with MAX(scored_at)
    # so the frontend can render a "scored N days ago / rescore now"
    # affordance without a second round-trip. Before this, the Resume
    # Score page had no way to tell the user their scores were stale
    # (11 days of stale scores in prod prior to the nightly schedule
    # fix). The nightly `rescore_all_active_resumes` keeps scored_at
    # current on active users but freshness visibility is still useful
    # for the "first scan is pending" window after a new upload and
    # for any user whose rescore task failed silently.
    score_stats = (await db.execute(
        select(
            func.count(ResumeScore.id),
            func.avg(ResumeScore.overall_score),
            func.max(ResumeScore.overall_score),
            func.max(ResumeScore.scored_at),
        ).where(ResumeScore.resume_id == resume.id)
    )).one()

    above_70 = (await db.execute(
        select(func.count(ResumeScore.id))
        .where(ResumeScore.resume_id == resume.id, ResumeScore.overall_score >= 70)
    )).scalar() or 0

    # Cheap NULL probe — same pattern as ``list_resumes``. Avoids
    # un-deferring the BYTEA column on the main row load.
    has_file_data = (await db.execute(
        select(Resume.id).where(
            Resume.id == resume.id,
            Resume.file_data.isnot(None),
        )
    )).scalar_one_or_none() is not None

    return {
        "active_resume": {
            "id": str(resume.id),
            "label": resume.label,
            "filename": resume.filename,
            "file_type": resume.file_type,
            "word_count": resume.word_count,
            "status": resume.status,
            "uploaded_at": resume.uploaded_at.isoformat(),
            "has_file_data": has_file_data,
            "score_summary": {
                "jobs_scored": score_stats[0] or 0,
                "average_score": round(float(score_stats[1]), 1) if score_stats[1] else 0.0,
                "best_score": round(float(score_stats[2]), 1) if score_stats[2] else 0.0,
                "above_70": above_70,
                # F96: ISO timestamp of the most recent ResumeScore row
                # for this resume, or None if no scores have been
                # written yet (fresh upload pending the scoring task).
                # The frontend renders this as "Scored 2h ago" / "Scored
                # 11 days ago — rescore now" / "Scoring…" depending on
                # age.
                "last_scored_at": (
                    score_stats[3].isoformat() if score_stats[3] else None
                ),
            },
        }
    }


@router.get("/{resume_id}/file")
async def get_resume_file(
    resume_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stream the original uploaded resume bytes for in-app preview.

    Loaded by the Resume Score page Preview modal — PDFs render in an
    iframe (browsers handle ``application/pdf`` inline by default),
    DOCX rows trigger a download (no native browser viewer for DOCX).

    Auth: cookie JWT — same-origin iframe inherits credentials, so the
    frontend can use this URL directly as ``<iframe src=...>``.

    Returns 410 Gone when ``file_data IS NULL`` (legacy rows uploaded
    before b8c9d0e1f2g3 added the column). The frontend translates 410
    into a "Re-upload to enable preview" hint plus a fallback view of
    the extracted ``text_content``.

    ``Content-Disposition: inline`` keeps the PDF rendering in-place;
    the filename hint is only used if the user picks "Save as".
    """
    # Need to un-defer ``file_data`` for THIS query only — the model
    # default is deferred so list/active queries don't pay the BYTEA
    # cost.
    resume = (await db.execute(
        select(Resume)
        .options(undefer(Resume.file_data))
        .where(Resume.id == resume_id, Resume.user_id == user.id)
    )).scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not resume.file_data:
        # 410 Gone — distinguishes "this resume exists but its bytes
        # aren't stored" from "no such resume" (404). Lets the UI render
        # the right empty state.
        raise HTTPException(
            status_code=410,
            detail="Original file is not stored for this resume. "
                   "Re-upload to enable preview.",
        )

    media_type = (
        "application/pdf"
        if resume.file_type == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # ``inline`` so PDFs render in-iframe; ``filename`` only kicks in
    # when the user explicitly downloads (Ctrl+S / "Save as").
    safe_name = (resume.filename or f"resume.{resume.file_type}").replace('"', "")
    return Response(
        content=resume.file_data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            # The bytes never change for a given resume id (uploads are
            # immutable — re-upload creates a new row). Letting the
            # browser cache aggressively makes preview re-opens instant.
            # Private + must-revalidate so a shared CDN can't serve one
            # user's resume to another.
            "Cache-Control": "private, max-age=3600, must-revalidate",
        },
    )


@router.get("/{resume_id}/text")
async def get_resume_text(
    resume_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the extracted plaintext used by the scorer.

    Powers two flows:

      * Fallback for legacy resumes (no ``file_data``) — the Preview
        modal falls back to this when ``GET /resume/{id}/file`` returns
        410.
      * Operator debugging — when a score looks wrong, knowing exactly
        what plain text the scoring pipeline saw is the fastest path
        to "the PDF extractor mangled this section".

    The full ``text_content`` lives on the row already; this is just a
    convenient JSON shape and a safe place to apply per-user auth.
    """
    resume = (await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )).scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    return {
        "id": str(resume.id),
        "filename": resume.filename,
        "file_type": resume.file_type,
        "word_count": resume.word_count,
        "text": resume.text_content or "",
    }


@router.patch("/{resume_id}/label")
async def update_resume_label(
    resume_id: UUID,
    body: ResumeLabelUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a resume's display label.

    F132: request body is now Pydantic-validated — non-string `label`
    values, nulls, empty strings, whitespace-only strings, and
    >100-char strings all 422 at parse time with a clear message
    instead of leaking a 500 stack trace. The old manual
    `body.get("label", "").strip()` dance + `label[:100]` truncation
    is gone; the schema enforces everything.
    """
    result = await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )
    resume = result.scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    # `body.label` is already stripped + length-checked by the schema.
    resume.label = body.label
    db.add(resume)
    await db.commit()

    return {"id": str(resume.id), "label": resume.label}


@router.get("")
async def list_resumes(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    all_users: bool = False,
):
    """List resumes. Admin/super_admin can pass ?all_users=true to see all.

    Regression finding 205: this endpoint is intentionally unpaginated
    because a user typically has 1-5 resumes (admin all_users view tops
    out at a few hundred platform-wide). We still emit the canonical
    pagination envelope keys (`total`, `page`, `page_size`, `total_pages`)
    so shared frontend components (`<PaginatedList>`) don't render a
    broken pager. `page_size` is always equal to `total` — a stable
    signal to callers that client-side pagination isn't needed. If the
    platform ever has >200 resumes per user, swap this for real
    pagination driven by `page`/`page_size` query params.
    """
    query = select(Resume).where(Resume.status != "archived")
    if all_users and user.role in ("admin", "super_admin"):
        # Admin sees all resumes across users
        query = query.options(joinedload(Resume.owner))
    else:
        query = query.where(Resume.user_id == user.id)
    query = query.order_by(Resume.uploaded_at.desc())

    result = await db.execute(query)
    resumes = result.unique().scalars().all()
    # Cheap "do we have the original bytes?" probe — selects only the
    # ids that have a non-NULL ``file_data`` so the iteration below can
    # set ``has_file_data`` without un-deferring the BYTEA column on
    # every row (which would defeat the whole point of ``deferred=True``
    # on the model). Two SELECTs vs. dragging the bytes through the
    # serializer is the right trade.
    has_file_ids: set[str] = set()
    if resumes:
        ids = [r.id for r in resumes]
        rows = (await db.execute(
            select(Resume.id).where(
                Resume.id.in_(ids),
                Resume.file_data.isnot(None),
            )
        )).scalars().all()
        has_file_ids = {str(rid) for rid in rows}

    items = [
        {
            "id": str(r.id),
            "label": r.label,
            "filename": r.filename,
            "file_type": r.file_type,
            "word_count": r.word_count,
            "status": r.status,
            "uploaded_at": r.uploaded_at.isoformat(),
            "is_active": str(user.active_resume_id) == str(r.id) if user.active_resume_id else False,
            # Tells the UI whether the Preview button can render an
            # iframe of the original file. Legacy rows uploaded before
            # b8c9d0e1f2g3 fall back to an extracted-text view in the
            # frontend modal.
            "has_file_data": str(r.id) in has_file_ids,
            **({"owner_name": r.owner.name, "owner_email": r.owner.email}
               if all_users and user.role in ("admin", "super_admin") and hasattr(r, "owner") and r.owner else {}),
        }
        for r in resumes
    ]
    total = len(items)
    return {
        "items": items,
        # F205: unified envelope keys — even though this list is
        # effectively unpaginated, emit the canonical shape so shared
        # frontend components don't render a broken `0 of 0` pager.
        "total": total,
        "page": 1,
        "page_size": total if total > 0 else 1,
        "total_pages": 1,
        "active_resume_id": str(user.active_resume_id) if user.active_resume_id else None,
    }


@router.delete("/{resume_id}")
async def archive_resume(
    resume_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Archive a resume (soft-delete — data is preserved)."""
    result = await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )
    resume = result.scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    resume.status = "archived"
    # Clear active if this was the active resume
    if user.active_resume_id and str(user.active_resume_id) == str(resume.id):
        user.active_resume_id = None
    await db.commit()
    return {"status": "archived", "message": "Resume archived (data preserved)"}


@router.post("/{resume_id}/score")
async def score_resume(
    resume_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dispatch background task to score resume against ALL relevant jobs."""
    from app.workers.tasks.resume_score_task import score_resume_task

    result = await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )
    resume = result.scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    if resume.status != "ready":
        raise HTTPException(status_code=400, detail="Resume text extraction failed. Please upload a text-based PDF or DOCX.")

    # Dispatch to Celery
    task = score_resume_task.delay(str(resume.id))

    return {
        "task_id": task.id,
        "resume_id": str(resume.id),
        "status": "scoring",
        "message": "Scoring against all relevant jobs. This may take a moment.",
    }


@router.get("/{resume_id}/score-status/{task_id}")
async def get_score_task_status(
    resume_id: UUID,
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll the status of a resume scoring task.

    Regression finding 204: `resume_id` was declared on the path but never
    referenced in the handler — any authenticated user could poll ANY
    task_id and receive its result, turning a 128-bit task_id from a
    routing key into a bearer token for cross-resume status info-leak
    (jobs_scored count, completion time). Two layers of defense now:

      1. Require that `resume_id` exists AND belongs to the caller.
         Someone with no resumes (or probing a different user's resume
         by fabricated UUID) gets a generic 404 before Celery is
         touched — matching the 404 semantics of every other
         `/resume/{id}/...` endpoint.

      2. Cross-validate that the task was actually dispatched by this
         resume. `score_resume` calls `score_resume_task.delay(str(resume.id))`
         so the first positional arg IS the resume_id. With
         `result_extended=True` in celery_app.conf (enabled alongside
         this fix) the args are persisted on the result row, so we can
         reject a task_id that belongs to a different resume. Older
         tasks queued before the config change won't carry args; for
         those we fall back to the ownership check alone, which still
         closes the attacker-with-no-resumes case.
    """
    owned = await db.execute(
        select(Resume.id).where(Resume.id == resume_id, Resume.user_id == user.id)
    )
    if owned.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Resume not found")

    from celery.result import AsyncResult
    result = AsyncResult(task_id)

    # Cross-validate: if celery recorded the task args (requires
    # `result_extended=True`), ensure the task was dispatched by THIS
    # resume. Don't 500 if `.args` raises (older celery versions, broker
    # stripped, etc.) — treat it as "can't verify, allow through" which
    # still has the ownership check above as a backstop.
    try:
        task_args = result.args
    except Exception:
        task_args = None
    if task_args and len(task_args) >= 1 and str(task_args[0]) != str(resume_id):
        # Generic 404 so a probe can't distinguish "task belongs to
        # a different resume of mine" from "task doesn't exist".
        raise HTTPException(status_code=404, detail="Task not found")

    if result.state == "PENDING":
        return {"status": "pending", "current": 0, "total": 0}
    elif result.state == "PROGRESS":
        info = result.info or {}
        return {"status": "progress", "current": info.get("current", 0), "total": info.get("total", 0)}
    elif result.state == "SUCCESS":
        info = result.result or {}
        return {
            "status": "completed",
            "jobs_scored": info.get("jobs_scored", 0),
            "total": info.get("total", 0),
            "error": info.get("error"),
        }
    elif result.state == "FAILURE":
        return {"status": "failed", "error": str(result.info)}
    else:
        return {"status": result.state.lower()}


@router.get("/{resume_id}/scores")
async def get_resume_scores(
    resume_id: UUID,
    # Regression finding 224: previously `page: int = 1, page_size: int = 25`
    # had NO `Query(..., ge=, le=)` bounds, so:
    #   - `?page_size=10000` materialized ~5,000 score rows × 2 keys
    #     (`items` + the F205 `scores` alias) = ~9-18 MB JSON in memory
    #     before emitting — a trivial memory-DoS lever on any authenticated
    #     JWT with at least one resume.
    #   - `?page=0` computed `offset = (0-1)*25 = -25` → PG 500 on
    #     negative OFFSET.
    #   - `?page=-1` / `?page_size=-1` hit the same negative-offset path
    #     and the PG `LIMIT -1` rejection — both surface as 500 Internal
    #     Server Error instead of a structured 422.
    #   - `sort_by`/`sort_dir` were raw `str` so `?sort_by=junk` silently
    #     fell through to the default ("overall_score") with no 422
    #     telling the caller the param was ignored. `sort_dir=foo`
    #     defaulted to `desc` the same way.
    # Fix mirrors F179 (/analytics/trends), F217 (/platforms/scan-logs),
    # F223 (/platforms/boards): strict `Query(..., ge=1, le=N)` bounds
    # on the pagination pair, `Literal` on the enum-like sort params so
    # typos return 422, and score bounds 0..100 matching the data domain
    # (overall_score is always 0..100 per `_ats_scoring.py`). `role_cluster`
    # stays free-form `str` because it's matched against the dynamic
    # `RoleClusterConfig` catalog that admins mutate — a Literal would
    # drift; if that becomes a problem, swap to DB-validation like the
    # /export/jobs?role_cluster pattern.
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    role_cluster: str | None = None,
    min_score: float | None = Query(None, ge=0, le=100),
    max_score: float | None = Query(None, ge=0, le=100),
    search: str | None = None,
    sort_by: Literal[
        "overall_score",
        "keyword_score",
        "role_match_score",
        "format_score",
        "job_title",
        "company_name",
    ] = "overall_score",
    sort_dir: Literal["asc", "desc"] = "desc",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get previously computed scores for a resume with pagination and filters."""
    result = await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )
    resume = result.scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    from app.models.company import Company

    # F311 (data correctness, companion to F310): filter the visible
    # resume-scores list to ACTIVE job statuses so users don't see
    # scores against expired or archived jobs they can't apply to.
    # Same status set the rescore task uses
    # (``"new", "under_review", "accepted"``) so the user-visible
    # list stays consistent with what was scored. Stale rows for
    # jobs that were active when scored and have since flipped to
    # expired/archived are filtered here, and the next rescore's
    # F300 cleanup pass deletes the underlying rows.
    _ACTIVE_JOB_STATUSES = ("new", "under_review", "accepted")

    # Build query with joins for filtering (join Company for search/sort)
    base_query = (
        select(ResumeScore, Job, Company.name.label("co_name"))
        .join(Job, ResumeScore.job_id == Job.id)
        .join(Company, Job.company_id == Company.id)
        .where(
            ResumeScore.resume_id == resume.id,
            Job.status.in_(_ACTIVE_JOB_STATUSES),
        )
    )

    # Apply filters
    if role_cluster:
        base_query = base_query.where(Job.role_cluster == role_cluster)
    if min_score is not None:
        base_query = base_query.where(ResumeScore.overall_score >= min_score)
    if max_score is not None:
        base_query = base_query.where(ResumeScore.overall_score <= max_score)
    if search and search.strip():
        # Findings 84+85: escape LIKE metachars + drop whitespace-only input
        # so `"100%"`, `"dev_ops"`, and `"   "` no longer return wildcard
        # matches in the resume-score search.
        search_term = f"%{escape_like(search.strip())}%"
        base_query = base_query.where(
            (Job.title.ilike(search_term, escape="\\"))
            | (Company.name.ilike(search_term, escape="\\"))
        )

    # Get total count (unfiltered for summary stats)
    all_scores_result = await db.execute(
        select(func.count(ResumeScore.id), func.avg(ResumeScore.overall_score))
        .where(ResumeScore.resume_id == resume.id)
    )
    all_row = all_scores_result.one()
    total_all = all_row[0] or 0
    avg_score_all = float(all_row[1]) if all_row[1] else 0.0

    # Get filtered count
    count_query = select(func.count()).select_from(base_query.subquery())
    total_filtered = (await db.execute(count_query)).scalar() or 0

    # Sort
    sort_col = {
        "overall_score": ResumeScore.overall_score,
        "keyword_score": ResumeScore.keyword_score,
        "role_match_score": ResumeScore.role_match_score,
        "format_score": ResumeScore.format_score,
        "job_title": Job.title,
        "company_name": Company.name,
    }.get(sort_by, ResumeScore.overall_score)

    if sort_dir == "asc":
        base_query = base_query.order_by(sort_col.asc())
    else:
        base_query = base_query.order_by(sort_col.desc())

    # Paginate
    offset = (page - 1) * page_size
    paginated_query = base_query.offset(offset).limit(page_size)

    rows = (await db.execute(paginated_query)).all()

    scores = []
    for score_record, job, co_name in rows:
        scores.append({
            "id": str(score_record.id),
            "job_id": str(score_record.job_id),
            "job_title": job.title,
            "company_name": co_name or "",
            "role_cluster": job.role_cluster or "",
            "overall_score": score_record.overall_score,
            "keyword_score": score_record.keyword_score,
            "role_match_score": score_record.role_match_score,
            "format_score": score_record.format_score,
            "matched_keywords": score_record.matched_keywords,
            "missing_keywords": score_record.missing_keywords,
            "suggestions": score_record.suggestions,
            "scored_at": score_record.scored_at.isoformat(),
        })

    # Get score distribution for summary (from all scores, not filtered)
    above_70 = (await db.execute(
        select(func.count(ResumeScore.id))
        .where(ResumeScore.resume_id == resume.id, ResumeScore.overall_score >= 70)
    )).scalar() or 0

    best_score = (await db.execute(
        select(func.max(ResumeScore.overall_score))
        .where(ResumeScore.resume_id == resume.id)
    )).scalar() or 0

    # Top missing keywords (from all scores)
    all_missing_result = await db.execute(
        select(ResumeScore.missing_keywords)
        .where(ResumeScore.resume_id == resume.id)
    )
    all_missing: dict[str, int] = {}
    for (mkw,) in all_missing_result:
        if mkw:
            for kw in mkw:
                all_missing[kw] = all_missing.get(kw, 0) + 1
    top_missing = [kw for kw, _ in sorted(all_missing.items(), key=lambda x: -x[1])[:10]]

    total_pages = (total_filtered + page_size - 1) // page_size if total_filtered > 0 else 1

    # Regression finding 205: every other paginated list endpoint in the
    # app returns the array under `items` (see `jobs.py`, `reviews.py`,
    # `discovery.py:49` which has the F108 comment "unified pagination
    # keys"). This endpoint used to emit `scores`, so the shared
    # `<PaginatedList>` / `<Pagination>` components rendered empty
    # silently. Emit `items` as the canonical key; keep `scores` as a
    # deprecated alias for ONE release so existing frontends don't break
    # mid-rollout. A follow-up round should grep for `\.scores` reads in
    # frontend/src and migrate the call sites, then delete the alias.
    # `total` (the filtered row count, matching the canonical envelope)
    # is emitted alongside the legacy `total_filtered` key for the same
    # reason.
    return {
        "resume_id": str(resume.id),
        "items": scores,
        "scores": scores,  # deprecated alias — see F205; drop next release
        "total": total_filtered,
        "average_score": round(avg_score_all, 1),
        "best_score": round(best_score, 1),
        "above_70": above_70,
        "top_missing_keywords": top_missing,
        "jobs_scored": total_all,
        "total_filtered": total_filtered,  # deprecated alias for `total`
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/ai-usage")
async def get_ai_usage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's AI customization usage for today.

    Regression finding 170: previously the rate-limit counter counted
    every `AICustomizationLog` row for today regardless of `success`.
    With `ANTHROPIC_API_KEY` unset, `customize_resume()` returns an
    error payload — the handler still logged a row and the counter
    incremented, locking the user out after 10 failed calls they never
    benefited from. Filter to `success=True` so the quota tracks
    actual AI work completed.

    F236: response now includes a `features` block with all three
    AI features (customize / cover_letter / interview_prep) plus the
    legacy top-level customize keys for backwards compatibility with
    existing frontend code (`lib/api.ts::getAIUsage`). New frontend
    code should read from `features[<feature>]` so adding a fourth
    AI feature is a config change, not a frontend type change.
    """
    from app.utils.ai_rate_limit import usage_snapshot
    from app.models.resume import AI_FEATURE_CUSTOMIZE

    snap = await usage_snapshot(db, user)
    cust = snap["features"][AI_FEATURE_CUSTOMIZE]
    return {
        # Legacy keys (customize-only) preserved for backwards
        # compatibility with the current ResumeScorePage UI.
        "used_today": cust["used"],
        "daily_limit": cust["limit"],
        "remaining": cust["remaining"],
        "has_api_key": snap["has_api_key"],
        # F236 new keys.
        "reset_at_utc": snap["reset_at_utc"],
        "features": snap["features"],
    }


@router.post("/{resume_id}/customize")
async def customize_resume_for_job(
    resume_id: UUID,
    body: CustomizeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI-powered resume customization for a specific job.

    Body: { job_id: UUID, target_score: int (60-95, default 85) }

    Regression finding 90: previously `body: dict` with a manual
    `if not (60 <= target_score <= 95)` guard. A string value such as
    `target_score="high"` raised a `TypeError` inside the comparison
    that surfaced as a 500. `CustomizeRequest` now enforces the bounds
    at parse time (Pydantic returns 422 on bad input) so the request
    never reaches the handler with a non-int target_score.
    """
    from app.workers.tasks._ai_resume import customize_resume
    from app.models.resume import AICustomizationLog
    from app.config import get_settings

    # `job_id` comes out of Pydantic as `UUID`; downstream code
    # (SQLAlchemy `.where(Job.id == job_id)`) accepts UUID directly,
    # but the `.delay(str(job_id), …)` call into Celery serializes a
    # string, so we normalize once here.
    job_id = body.job_id
    target_score = body.target_score

    settings = get_settings()

    # Regression finding 203: tester reports live `used_today` still
    # increments on API-key-missing errors despite the round-15 fix.
    # Code inspection confirms the handler already sets `success=False`
    # on error rows and the quota query already filters
    # `success==True` — so this is EITHER (a) deploy drift (older
    # image running without the filter) OR (b) an accidentally-True
    # row slipping through via the model's `default=True`. Defense-
    # in-depth: short-circuit BEFORE any DB work when the API key is
    # unset, so no log row can be written and no quota state can
    # mutate regardless of what downstream code does. Returning the
    # same 200 + `error:true` shape the frontend `ResumeScorePage`
    # already renders (see `customization?.error &&` branch at
    # line 824) so the UX doesn't regress.
    #
    # Tester item (5) on F203: a shared `AIConfiguredDependency` that
    # 503s early would be cleaner than per-endpoint short-circuits,
    # but that would break the inline-error UX on this page. Keep the
    # 200+error shape here; use the dependency for future endpoints
    # that don't have the same "render the error inline" UI.
    if not settings.anthropic_api_key.get_secret_value():
        # Count quota for the response usage block (reads only, no
        # write). Zero delta — the quota stays where it was.
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        used_today_ro = (await db.execute(
            select(func.count(AICustomizationLog.id))
            .where(
                AICustomizationLog.user_id == user.id,
                AICustomizationLog.created_at >= today_start,
                AICustomizationLog.success == True,  # noqa: E712
            )
        )).scalar() or 0
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "F203: refused /resume/%s/customize — ANTHROPIC_API_KEY not configured (user=%s)",
            resume_id, user.id,
        )
        return {
            "resume_id": str(resume_id),
            "job_id": str(job_id),
            "job_title": "",
            "target_score": target_score,
            "customized_text": "",
            "changes_made": [],
            "improvement_notes": "AI customization requires an Anthropic API key. Please configure ANTHROPIC_API_KEY.",
            "error": True,
            "usage": {
                "used_today": used_today_ro,
                "daily_limit": settings.ai_daily_limit_per_user,
                "remaining": max(0, settings.ai_daily_limit_per_user - used_today_ro),
            },
        }

    # F236: lifted the rate-limit + audit-log into the shared
    # `app.utils.ai_rate_limit` helper so all three AI handlers
    # (customize, cover-letter, interview-prep) use the same code path.
    # Behavior is unchanged from the original F170 inline implementation:
    # only success=True rows count toward the daily quota; 429 with
    # Retry-After when the cap hits.
    from app.utils.ai_rate_limit import (
        check_ai_quota, log_ai_call, usage_snapshot,
    )
    from app.models.resume import AI_FEATURE_CUSTOMIZE
    used_today = await check_ai_quota(db, user, AI_FEATURE_CUSTOMIZE)

    # Load resume
    result = await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )
    resume = result.scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    # Load job
    job = (await db.execute(
        select(Job).options(joinedload(Job.description)).where(Job.id == job_id)
    )).unique().scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Restrict to relevant jobs only
    relevant_clusters = await _get_relevant_clusters(db)
    if job.role_cluster not in relevant_clusters:
        raise HTTPException(
            status_code=400,
            detail="AI resume customization is only available for relevant jobs (infra, security, etc.)"
        )

    # Get existing score if available.
    # F225-followup: resume_scores has no UNIQUE (resume_id, job_id) and
    # we routinely end up with multiple rows for the same pair (concurrent
    # score_resume_task + rescore_all_active_resumes + manual rescore
    # paths interleave). The old `scalar_one_or_none()` would raise
    # `MultipleResultsFound` and surface as a 500 to the user — exactly
    # the failure mode that was breaking GET /jobs/{id} on the Bitwarden
    # Senior Security Engineer row. Pick the most recent row until the
    # underlying dedupe + UNIQUE-constraint migration lands.
    existing_score = (await db.execute(
        select(ResumeScore)
        .where(
            ResumeScore.resume_id == resume.id,
            ResumeScore.job_id == job.id,
        )
        .order_by(ResumeScore.scored_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    matched_keywords = existing_score.matched_keywords if existing_score else []
    missing_keywords = existing_score.missing_keywords if existing_score else []

    # If no existing score, compute one
    if not existing_score:
        desc_text = job.description.text_content if job.description else ""
        ats_result = compute_ats_score(
            resume_text=resume.text_content,
            job_title=job.title,
            matched_role=job.matched_role,
            role_cluster=job.role_cluster,
            description_text=desc_text,
        )
        matched_keywords = ats_result["matched_keywords"]
        missing_keywords = ats_result["missing_keywords"]

    desc_text = job.description.text_content if job.description else ""

    # Call AI customization
    ai_result = customize_resume(
        resume_text=resume.text_content,
        job_title=job.title,
        job_description=desc_text,
        matched_keywords=matched_keywords,
        missing_keywords=missing_keywords,
        target_score=target_score,
    )

    # F236: shared audit-log helper. Same semantics as before — both
    # success and failure rows persist (so operators can debug failure
    # rates) but only success=True rows count toward the daily quota
    # via the shared `count_ai_calls_today` filter.
    succeeded = not ai_result.get("error", False)
    await log_ai_call(
        db, user, AI_FEATURE_CUSTOMIZE,
        resume_id=resume.id,
        job_id=job.id,
        input_tokens=ai_result.get("input_tokens", 0),
        output_tokens=ai_result.get("output_tokens", 0),
        success=succeeded,
    )

    # F238: training-data capture for customize_quality. Side-effect.
    if succeeded:
        try:
            from app.utils.training_capture import capture_customize_quality
            await capture_customize_quality(
                db,
                user_id=user.id,
                resume_text=resume.text_content,
                job_title=job.title,
                job_description=desc_text,
                customized_text=ai_result.get("customized_text", ""),
                target_score=target_score,
                job_id=job.id,
                model_version="claude-sonnet-4-20250514",
            )
        except Exception:
            pass

    # F236: usage snapshot uses the same canonical helper that powers
    # /api/v1/ai/usage so the customize-only `used_today/daily_limit/
    # remaining` keys stay backwards-compatible while the new per-
    # feature breakdown arrives via the snapshot's `features` block.
    snap = await usage_snapshot(db, user)
    cust = snap["features"][AI_FEATURE_CUSTOMIZE]
    return {
        "resume_id": str(resume.id),
        "job_id": str(job.id),
        "job_title": job.title,
        "target_score": target_score,
        "customized_text": ai_result["customized_text"],
        "changes_made": ai_result["changes_made"],
        "improvement_notes": ai_result["improvement_notes"],
        "error": ai_result.get("error", False),
        # Backwards-compatible top-level keys (existing
        # `lib/api.ts::customizeResume` reads these). The new
        # `features` map mirrors the /ai/usage envelope so a future
        # frontend can read both numbers from one place.
        "usage": {
            "used_today": cust["used"],
            "daily_limit": cust["limit"],
            "remaining": cust["remaining"],
        },
    }
