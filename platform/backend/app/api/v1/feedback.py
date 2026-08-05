"""Feedback API — sales team submits bugs/features, admin manages directly."""

import json
import os
import uuid
from uuid import UUID
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy import select, func, desc, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.feedback import Feedback
from app.api.deps import get_current_user, require_role
from app.schemas.feedback import (
    FeedbackCreate,
    FeedbackOut,
    FeedbackUpdate,
    VALID_CATEGORIES,
    VALID_PRIORITIES,
    VALID_STATUSES,
)
from app.utils.sql import escape_like

router = APIRouter(prefix="/feedback", tags=["feedback"])

UPLOAD_DIR = Path("/app/uploads/feedback")
# Don't mkdir at import time — that crashes `python -c "from app.main import
# app"` on any env where `/app` is read-only, which is every CI runner
# (GH Actions workspace is /home/runner/work, the precheck job never has
# /app writable). The Round 28+ deploy pipeline's "Backend import smoke
# test" silently failed for ~5 hours because of this one line, blocking
# every merge to main from landing on the VM — including the GET /jobs/{id}
# hotfix users were waiting on. Defer the mkdir to the upload handler,
# where `/app/uploads` already exists (bind-mounted in docker-compose)
# so the cost is one `exists_ok=True` call per request, not zero cost at
# import vs. an entire broken deploy pipeline.
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # CI / read-only env: handler will retry on first upload
    pass
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Regression finding 189: previously included "image/svg+xml". SVG is
# XML and can carry <script> — a user could upload an SVG payload and
# (if the frontend ever renders it inline) achieve stored XSS. Plain
# bitmap images + PDFs + Office docs + CSV/plaintext are enough for
# the use-case (screenshot, log file, sample spreadsheet); SVG was a
# latent vulnerability with no real UX win.
ALLOWED_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "application/pdf",
    "text/plain", "text/csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Regression finding 189: the previous upload path trusted the
# client-declared `Content-Type`. A user could send a JS file with
# `Content-Type: application/pdf` or an HTML payload with `image/png`
# and the server would happily store it as-is — a classic stored-XSS
# precursor if the file is ever served inline. We now sniff magic
# bytes on the first 16 bytes of the upload and reject when the
# sniffed type doesn't match the claimed Content-Type.
#
# Mapping is magic-bytes → the set of Content-Type values that a
# real file with those bytes could legitimately carry. Kept inline
# (no `python-magic` dep) because the set of allowed types is small
# and all have cheap, unambiguous signatures.
_MAGIC_BYTES = [
    # (signature, allowed_content_types)
    (b"\x89PNG\r\n\x1a\n", {"image/png"}),
    (b"\xff\xd8\xff", {"image/jpeg"}),
    (b"GIF87a", {"image/gif"}),
    (b"GIF89a", {"image/gif"}),
    (b"RIFF", {"image/webp"}),  # WebP begins with "RIFF....WEBP" — chunk check below
    (b"%PDF-", {"application/pdf"}),
    (b"PK\x03\x04", {
        # DOCX and XLSX are both ZIP packages. We accept either; the
        # claimed Content-Type disambiguates which one the client
        # meant, and both signatures are legal.
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }),
]


def _sniff_content_type(content: bytes, claimed: str) -> bool:
    """Return True when the file's magic bytes are consistent with `claimed`.

    F189: reject MIME spoofing. text/plain and text/csv don't have a
    magic-byte signature (they're literally just text) so we accept
    them only after confirming the body decodes as UTF-8 and contains
    no NUL bytes — a cheap-but-effective heuristic that keeps
    binary-as-text spoofs out without a full format parser.
    """
    if claimed in ("text/plain", "text/csv"):
        if b"\x00" in content[:4096]:
            return False
        try:
            content[:4096].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False

    for signature, allowed_claimed in _MAGIC_BYTES:
        if content.startswith(signature):
            # Extra WebP discriminator — "RIFF" alone isn't enough
            # since WAV / AVI also start with RIFF. Require the
            # "WEBP" chunk marker at offset 8.
            if signature == b"RIFF":
                if len(content) < 12 or content[8:12] != b"WEBP":
                    continue
            return claimed in allowed_claimed
    return False


def _sanitize_original_name(name: str) -> str:
    """Strip path-traversal characters from the client-provided filename.

    F189: the previous code stored `original_name` verbatim, so a
    filename like `../../etc/passwd` was persisted as-is. No traversal
    at write time (the on-disk name is a fresh UUID), but this leaks
    a confusing string into admin UIs and log viewers, and plays
    badly with any future download-with-original-filename feature.
    """
    if not name:
        return "unnamed"
    # Strip any directory segments — keep only the final path
    # component, then drop leading dots to kill "..", "...", etc.
    bare = Path(name).name.lstrip(".")
    return bare[:255] if bare else "unnamed"


@router.post("", response_model=FeedbackOut)
async def create_feedback(
    body: FeedbackCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit feedback (any authenticated user)."""
    if body.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"Invalid category. Must be one of: {', '.join(VALID_CATEGORIES)}")
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"Invalid priority. Must be one of: {', '.join(VALID_PRIORITIES)}")

    if body.category == "bug":
        if not body.steps_to_reproduce:
            raise HTTPException(400, "Bug reports require 'steps_to_reproduce'")
        if not body.expected_behavior:
            raise HTTPException(400, "Bug reports require 'expected_behavior'")
        if not body.actual_behavior:
            raise HTTPException(400, "Bug reports require 'actual_behavior'")
    elif body.category == "feature_request":
        if not body.use_case:
            raise HTTPException(400, "Feature requests require 'use_case'")
        if not body.impact:
            raise HTTPException(400, "Feature requests require 'impact'")

    # Dedup — block same user from creating a duplicate open ticket with
    # identical (case-insensitive) title + category within the last 7 days.
    # Users were accidentally submitting 8+ identical "Resume Score / Relevance"
    # tickets (see regression test report Finding 11).
    dedup_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    dup_result = await db.execute(
        select(Feedback).where(
            Feedback.user_id == user.id,
            Feedback.category == body.category,
            func.lower(Feedback.title) == body.title.strip().lower(),
            Feedback.status.in_(("open", "in_progress")),
            Feedback.created_at >= dedup_cutoff,
        ).limit(1)
    )
    existing = dup_result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "You already have an open ticket with the same title. "
                    "Please add a comment there or wait for it to be resolved."
                ),
                "existing_feedback_id": str(existing.id),
                "existing_status": existing.status,
            },
        )

    fb = Feedback(
        user_id=user.id,
        category=body.category,
        priority=body.priority,
        title=body.title,
        description=body.description,
        steps_to_reproduce=body.steps_to_reproduce,
        expected_behavior=body.expected_behavior,
        actual_behavior=body.actual_behavior,
        use_case=body.use_case,
        proposed_solution=body.proposed_solution,
        impact=body.impact,
        screenshot_url=body.screenshot_url,
        attachments="[]",
    )
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    return fb


@router.post("/{feedback_id}/attachments")
async def upload_attachment(
    feedback_id: UUID,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file attachment to a feedback ticket."""
    fb = await db.get(Feedback, feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")

    # Only ticket author or admin can attach files
    if fb.user_id != user.id and user.role not in ("admin", "super_admin"):
        raise HTTPException(403, "Not authorized")

    # Validate file
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)} MB")

    content_type = file.content_type or "application/octet-stream"
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(400, f"File type '{content_type}' not allowed")

    # F189: the client-supplied Content-Type can't be trusted. Sniff
    # the magic bytes and reject when the body doesn't actually match
    # what the client claimed. Blocks JS-as-PDF / HTML-as-PNG uploads
    # that would otherwise sit in storage waiting for an inline
    # rendering path to turn into XSS.
    if not _sniff_content_type(content, content_type):
        raise HTTPException(
            400,
            "Uploaded file contents do not match the declared Content-Type",
        )

    # F189: sanitize the client-provided filename before we persist
    # it in the attachments JSON. Disk filename is already a UUID so
    # we're not preventing traversal at write time — this is about
    # keeping "../../etc/passwd" out of admin UI / audit logs.
    original_name = _sanitize_original_name(file.filename or "unnamed")

    # Save file. mkdir is idempotent and cheap (a no-op syscall when the
    # directory already exists) — call it here because the import-time
    # mkdir above is allowed to fail on CI read-only envs. On prod,
    # /app/uploads is a bind-mount that exists before the container
    # starts, so this is a confirmation, not the first creation.
    ext = Path(file.filename or "file").suffix or ""
    stored_name = f"{uuid.uuid4().hex}{ext}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_path = UPLOAD_DIR / stored_name
    file_path.write_bytes(content)

    # Update attachments JSON
    existing = json.loads(fb.attachments or "[]")
    existing.append({
        "filename": stored_name,
        "original_name": original_name,
        "size": len(content),
        "content_type": content_type,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    })
    fb.attachments = json.dumps(existing)
    await db.commit()
    await db.refresh(fb)

    return {"ok": True, "attachment": existing[-1], "total": len(existing)}


@router.delete("/{feedback_id}/attachments/{filename}")
async def delete_attachment(
    feedback_id: UUID,
    filename: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove an attachment from a feedback ticket."""
    fb = await db.get(Feedback, feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")

    if fb.user_id != user.id and user.role not in ("admin", "super_admin"):
        raise HTTPException(403, "Not authorized")

    existing = json.loads(fb.attachments or "[]")
    updated = [a for a in existing if a["filename"] != filename]
    if len(updated) == len(existing):
        raise HTTPException(404, "Attachment not found")

    # Delete file from disk
    file_path = UPLOAD_DIR / filename
    if file_path.exists():
        file_path.unlink()

    fb.attachments = json.dumps(updated)
    await db.commit()

    return {"ok": True}


@router.get("/attachments/{filename}")
async def get_attachment(
    filename: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serve an attachment file.

    Authorization: the caller must either own the feedback ticket that lists
    this filename in its attachments JSON, or be admin/super_admin. Previously
    this endpoint had no auth dependency at all — any anonymous request could
    download any attachment by guessing the UUID filename (regression #21).
    """
    # Sanitize filename to prevent directory traversal
    safe_name = Path(filename).name
    file_path = UPLOAD_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(404, "File not found")

    # Find the feedback row that owns this attachment. The `attachments`
    # column is a JSON-encoded string, so we LIKE-match the exact
    # "filename": "<name>" fragment to avoid partial matches. Finding 84:
    # escape LIKE metachars in `safe_name` so a filename containing a
    # literal `%` or `_` (legal on disk) doesn't wildcard-match a
    # different user's attachments row and return the wrong owner_id.
    like_needle = f'%"filename": "{escape_like(safe_name)}"%'
    owner_result = await db.execute(
        select(Feedback.user_id).where(Feedback.attachments.ilike(like_needle, escape="\\")).limit(1)
    )
    owner_id = owner_result.scalar_one_or_none()
    if owner_id is None:
        # File exists on disk but isn't linked to any feedback row —
        # treat as not-found rather than leaking its existence.
        raise HTTPException(404, "File not found")

    if owner_id != user.id and user.role not in ("admin", "super_admin"):
        raise HTTPException(403, "Not authorized")

    # F189: force download rather than inline rendering. Even after
    # MIME sniffing at upload time, we want the browser to treat the
    # blob as a file to save — closes the attack path where an HTML
    # or SVG payload persisted before the sniff fix still renders
    # when served via an <iframe> / <img> preview. `Content-
    # Disposition: attachment` is the standard defense.
    return FileResponse(
        file_path,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            # SECURITY (auth-bypass-via-CDN-cache): this endpoint is
            # per-user authorized, but its URL ends in the stored
            # filename's extension (…/attachments/<uuid>.png). Cloudflare
            # caches static extensions (.png/.jpg/.pdf/…) by default, so
            # WITHOUT an explicit no-cache directive the first authorized
            # fetch populated the edge cache and every subsequent
            # *unauthenticated* request for that URL was served the file
            # straight from Cloudflare — a full auth bypass on other
            # users' uploaded attachments (verified live: unauth-first
            # 401, then 200 after an authed fetch). ``private, no-store``
            # tells Cloudflare never to cache it; matches the
            # profile-document download handler (which was safe both
            # because it sends no-store AND has no extension in its URL).
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


@router.get("")
async def list_feedback(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    category: str | None = Query(None),
    status: str | None = Query(None),
    priority: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    # Regression finding 220(C): previously `sort_by` and `feedback_type`
    # were undeclared query params. The handler hardcoded
    # `order_by(desc(Feedback.created_at))` at every request, but clients
    # sending `?sort_by=priority` got 200 + default sort with NO signal
    # that their sort was ignored. Declaring the Literal here matches the
    # F198 pattern on /jobs (Literal-typed sort_by) — typos 422 with the
    # allowed columns listed, and legit values apply. Columns chosen
    # match Feedback model fields that are reasonable to sort by
    # (created_at, updated_at, priority, status); fields like
    # `description` or `title` are free-text and don't warrant sort UX.
    #
    # `feedback_type` has no matching column on the Feedback model (the
    # tester observed the UI sending it as a leftover from an earlier
    # design) so it's NOT added here — the right fix is to stop sending
    # it from the client, which the current FeedbackPage already does.
    # A cross-cutting forbid_extra middleware (F220 sketch D) would 422
    # any client still sending `feedback_type` or `per_page`; that's
    # orthogonal to this round.
    sort_by: Literal["created_at", "updated_at", "priority", "status"] = "created_at",
    sort_dir: Literal["asc", "desc"] = "desc",
):
    """List feedback. Admin/super_admin see all, others see only their own."""
    # Regression finding 162: filter params were being passed straight into the
    # `where()` clauses with no validation — `?category=bug'+OR+1=1--` returned
    # HTTP 200 (parameterised query absorbed it, so no SQLi), but invalid values
    # still reached the DB and produced confusing `total: 0` responses that
    # masked the typo from the caller. Rejecting unknown filter values with
    # 422 gives the client a clear signal and denies attackers a useful
    # zero-result probe.
    if category is not None and category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid category. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}",
        )
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
        )
    if priority is not None and priority not in VALID_PRIORITIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid priority. Must be one of: {', '.join(sorted(VALID_PRIORITIES))}",
        )

    query = select(Feedback)

    if user.role not in ("admin", "super_admin"):
        query = query.where(Feedback.user_id == user.id)

    if category:
        query = query.where(Feedback.category == category)
    if status:
        query = query.where(Feedback.status == status)
    if priority:
        query = query.where(Feedback.priority == priority)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # F220(C): Literal-validated sort_by → safe to look up by name on
    # the Feedback class (Pydantic guaranteed the string is one of the
    # allowed column names). `desc`/`asc` applied per sort_dir.
    sort_col = getattr(Feedback, sort_by)
    sort_expr = sort_col.desc() if sort_dir == "desc" else sort_col.asc()
    items_q = query.order_by(sort_expr).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(items_q)
    items = result.scalars().all()

    return {
        "items": [FeedbackOut.model_validate(i) for i in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/stats")
async def feedback_stats(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Feedback overview stats (admin/super_admin)."""
    result = await db.execute(
        select(Feedback.status, func.count()).group_by(Feedback.status)
    )
    by_status = dict(result.all())

    result2 = await db.execute(
        select(Feedback.category, func.count()).group_by(Feedback.category)
    )
    by_category = dict(result2.all())

    result3 = await db.execute(
        select(Feedback.priority, func.count())
        .where(Feedback.status.in_(["open", "in_progress"]))
        .group_by(Feedback.priority)
    )
    open_by_priority = dict(result3.all())

    return {
        "by_status": by_status,
        "by_category": by_category,
        "open_by_priority": open_by_priority,
        "total": sum(by_status.values()),
    }


@router.get("/{feedback_id}", response_model=FeedbackOut)
async def get_feedback(
    feedback_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get single feedback item."""
    # Path param is typed UUID so FastAPI returns 422 on a malformed id
    # instead of letting SQLAlchemy raise and bubble up as 500.
    fb = await db.get(Feedback, feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")

    if user.role not in ("admin", "super_admin") and fb.user_id != user.id:
        raise HTTPException(403, "Not authorized")

    return fb


@router.patch("/{feedback_id}", response_model=FeedbackOut)
async def update_feedback(
    feedback_id: UUID,
    body: FeedbackUpdate,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update feedback status/priority/notes (admin/super_admin only)."""
    fb = await db.get(Feedback, feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")

    if body.status:
        if body.status not in VALID_STATUSES:
            raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(VALID_STATUSES)}")
        fb.status = body.status
        if body.status == "resolved":
            fb.resolved_at = datetime.now(timezone.utc)
            fb.resolved_by = user.id

    if body.priority:
        if body.priority not in VALID_PRIORITIES:
            raise HTTPException(400, "Invalid priority")
        fb.priority = body.priority

    if body.admin_notes is not None:
        fb.admin_notes = body.admin_notes

    await db.commit()
    await db.refresh(fb)
    return fb
