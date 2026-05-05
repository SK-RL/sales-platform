"""Admin-only person-profile + KYC document vault endpoints.

All routes gate on ``require_role("admin")`` — which, via the role
hierarchy in ``app.api.deps.ROLE_HIERARCHY``, lets both ``admin`` and
``super_admin`` through. ``reviewer`` and ``viewer`` get a generic
403 with no role name in the body (F185 info-leak pattern).

Every successful access is recorded to ``audit_logs`` via
``log_action``. The metadata captures ``profile_id`` + the specific
action verb, NEVER the document bytes or the UAN/PAN values — audit
logs are queryable by less-privileged ops users and should not double
as a secondary leak surface.

Document upload / download mechanics
------------------------------------
* Upload uses ``multipart/form-data`` with a single ``file`` field
  plus text fields for ``doc_type`` + optional ``doc_label``.
* Size + MIME + magic-byte validation in
  ``app.utils.profile_doc_storage.validate_and_store``.
* Download streams via ``fastapi.responses.Response`` with
  ``Content-Disposition: attachment`` + the original filename (not
  the UUID-based storage path — a human-friendly name for the admin).
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal
from uuid import UUID

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import Response
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.database import get_db
from app.models.profile import Profile, ProfileDocument
from app.models.user import User
from app.schemas.profile import (
    DocType,
    DocumentUploadResult,
    ProfileCreate,
    ProfileDetailOut,
    ProfileDocumentOut,
    ProfileListItem,
    ProfileListResponse,
    ProfileOut,
    ProfileUpdate,
)
from app.utils.audit import log_action
from app.utils.profile_doc_storage import (
    DocValidationError,
    read_bytes,
    unlink_if_exists,
    validate_and_store,
)
from app.utils.sql import escape_like

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/profiles", tags=["profiles"])


# Centralise the role dep so each endpoint is a one-liner that
# the grep-ability tests can verify. Every route MUST use this —
# `get_current_user` alone would let reviewer/viewer through.
_ADMIN_GUARD = require_role("admin")


async def _get_profile_or_404(
    profile_id: UUID, db: AsyncSession, *, include_archived: bool = False
) -> Profile:
    """Load a Profile by id, 404 if missing or archived.

    ``include_archived=True`` lets the GET detail endpoint surface
    archived rows to admins who are reviewing past profiles — lists
    hide them by default.
    """
    q = select(Profile).where(Profile.id == profile_id)
    if not include_archived:
        q = q.where(Profile.archived_at.is_(None))
    row = (await db.execute(q)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    return row


# ── Profile CRUD ──────────────────────────────────────────────────


@router.get("", response_model=ProfileListResponse)
async def list_profiles(
    search: str | None = Query(default=None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    include_archived: bool = False,
    user: User = Depends(_ADMIN_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Paginated profile list. Searchable by name or email (case-
    insensitive substring). Archived rows hidden by default —
    ``include_archived=true`` opts in.
    """
    q = select(Profile)
    if not include_archived:
        q = q.where(Profile.archived_at.is_(None))

    if search and search.strip():
        # Escape % and _ so a literal "50%" search doesn't become a
        # wildcard. `lower()` on both sides for case-insensitive
        # match — uses the `ix_profiles_email_lower` functional index
        # for the email half.
        needle = f"%{escape_like(search.strip().lower())}%"
        q = q.where(
            or_(
                func.lower(Profile.name).ilike(needle, escape="\\"),
                func.lower(Profile.email).ilike(needle, escape="\\"),
            )
        )

    # Count before pagination — same subquery pattern used across the
    # API for consistent totals.
    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    q = q.order_by(Profile.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()

    # Attach document_count via a single aggregate query keyed on the
    # just-returned profile ids. N+1 avoided.
    ids = [r.id for r in rows]
    doc_counts: dict[uuid.UUID, int] = {}
    if ids:
        dc_q = (
            select(ProfileDocument.profile_id, func.count(ProfileDocument.id))
            .where(
                ProfileDocument.profile_id.in_(ids),
                ProfileDocument.archived_at.is_(None),
            )
            .group_by(ProfileDocument.profile_id)
        )
        doc_counts = {pid: n for pid, n in (await db.execute(dc_q)).all()}

    # F238(c): construct the slim ``ProfileListItem`` — the list
    # response deliberately omits UAN / PF / notes so a paginated
    # "show me everyone" call can't be used as a bulk-PII export.
    # Full fields remain on the per-id detail endpoint.
    items = []
    for row in rows:
        out = ProfileListItem.model_validate(row)
        out.document_count = doc_counts.get(row.id, 0)
        items.append(out)

    return ProfileListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, (total + page_size - 1) // page_size),
    )


@router.post("", response_model=ProfileOut, status_code=201)
async def create_profile(
    body: ProfileCreate,
    request: Request,
    user: User = Depends(_ADMIN_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Create a new profile. Email is unique — duplicate returns 409
    rather than silently upserting (upserting a KYC record is the
    kind of operation that should require an explicit admin decision).
    """
    # Check for dupe before insert so we return 409, not a raw
    # IntegrityError 500. The lookup is the fast path for serial
    # callers — the SAVEPOINT below closes the TOCTOU race when two
    # admins POST the same email simultaneously.
    existing = (await db.execute(
        select(Profile).where(func.lower(Profile.email) == body.email.lower())
    )).scalar_one_or_none()
    if existing:
        # F238(e) regression fix: don't leak the existing row's UUID in
        # the 409 body. The UUID is an internal handle that a caller
        # without list/read privileges shouldn't be able to fish out
        # just by probing emails. The error is still actionable — the
        # admin knows which email to dedupe — without giving away the
        # id of the pre-existing record.
        raise HTTPException(
            status_code=409,
            detail=f"A profile with email {body.email!r} already exists.",
        )

    profile = Profile(
        id=uuid.uuid4(),
        name=body.name,
        dob=body.dob,
        email=body.email,
        father_name=body.father_name,
        uan_number=body.uan_number,
        pf_number=body.pf_number,
        notes=body.notes,
        created_by_user_id=user.id,
    )
    db.add(profile)
    # F325 (race-safe): the lookup-then-insert above is TOCTOU-
    # vulnerable to two admins POSTing the same email at the same
    # instant — both pass the lookup, both INSERT, and the second
    # blows up with an unhandled ``IntegrityError`` that escapes as
    # a bare HTTP 500. The Profile model declares the named
    # constraint ``UniqueConstraint("email", name="uq_profiles_email")``
    # so we can match on the constraint name and raise the same 409
    # the lookup-check produces — user-visible outcome identical
    # regardless of timing. Other IntegrityErrors (FK violation,
    # check-constraint failure, etc.) propagate as 500 so genuinely-
    # different bugs aren't hidden behind this translation.
    #
    # Same shape as F281 (reviews), F282 (contacts), F316 (jobs),
    # F320 (potential-clients), F322 (answer-book), F323 (routine-
    # targets), F324 (work-time pending): SAVEPOINT-wrapped INSERT
    # + named-constraint IntegrityError catch.
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        diag = getattr(getattr(exc, "orig", None), "diag", None)
        constraint_name = (
            getattr(diag, "constraint_name", "") if diag else ""
        ) or ""
        if (
            "uq_profiles_email" in constraint_name
            or "uq_profiles_email" in str(exc).lower()
        ):
            raise HTTPException(
                status_code=409,
                detail=f"A profile with email {body.email!r} already exists.",
            )
        raise
    await db.refresh(profile)

    # F238(b) regression fix: NEVER put PII values (email / PAN / UAN)
    # into audit metadata. The audit log is queryable by less-privileged
    # ops users, so dumping the email here turned it into a secondary
    # leak surface — an ops user with read access to ``audit_logs``
    # could enumerate every profile's email without ever touching the
    # ``profiles`` table. Mirror the ``profile.update`` pattern: log
    # only the list of field names that were populated. The
    # ``profile_id`` stays so compliance can still correlate the action
    # with the row.
    await log_action(
        db, user,
        action="profile.create",
        resource="profile",
        request=request,
        metadata={
            "profile_id": str(profile.id),
            "fields": sorted(body.model_dump(exclude_unset=True).keys()),
        },
    )
    return ProfileOut.model_validate(profile)


@router.get("/{profile_id}", response_model=ProfileDetailOut)
async def get_profile(
    profile_id: UUID,
    request: Request,
    include_archived: bool = False,
    user: User = Depends(_ADMIN_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Profile detail + all attached documents (metadata only — file
    bytes live behind ``/documents/{id}/download``)."""
    profile = await _get_profile_or_404(profile_id, db, include_archived=include_archived)

    # Non-archived docs by default so the detail list doesn't look
    # cluttered after retention-related archives. Full history is
    # available via ``?include_archived=true``.
    doc_q = select(ProfileDocument).where(ProfileDocument.profile_id == profile.id)
    if not include_archived:
        doc_q = doc_q.where(ProfileDocument.archived_at.is_(None))
    doc_q = doc_q.order_by(ProfileDocument.uploaded_at.desc())
    docs = (await db.execute(doc_q)).scalars().all()

    # F239 (P0): construct the response via a plain dict + kwargs
    # rather than ``ProfileDetailOut.model_validate(profile)`` + post-
    # validation field assignment. Two independent problems with the
    # old form:
    #
    # 1. ``ProfileDetailOut`` inherits ``from_attributes=True``, and it
    #    declares a ``documents: list[ProfileDocumentOut]`` field. When
    #    Pydantic walks the attributes of ``profile``, it hits
    #    ``profile.documents`` — a SQLAlchemy relationship that wasn't
    #    eagerly loaded (``_get_profile_or_404`` doesn't
    #    ``selectinload``). In the async session context this triggers
    #    an implicit lazy-load, which raises ``MissingGreenlet`` and
    #    collapses to a bare "Internal Server Error" string with no
    #    JSON envelope — which is exactly the symptom tester F239
    #    reported (every call to this endpoint 500s, no stack in body,
    #    all sibling endpoints fine).
    # 2. Even if documents had been eagerly loaded, post-validation
    #    assignment like ``out.documents = [...]`` is brittle under
    #    Pydantic V2 once we add any kind of ``frozen`` / strict
    #    config — and it pointlessly revalidates on assignment.
    #
    # Building a dict from ``ProfileOut`` (which does NOT have a
    # ``documents`` field, so Pydantic never probes that attribute)
    # and then kwargs-constructing ``ProfileDetailOut`` sidesteps both.
    doc_outs = [ProfileDocumentOut.model_validate(d) for d in docs]
    base = ProfileOut.model_validate(profile).model_dump()
    base["documents"] = [d.model_dump() for d in doc_outs]
    base["document_count"] = len(doc_outs)
    out = ProfileDetailOut.model_validate(base)

    # Audit every read — KYC reads are sensitive enough that "who
    # looked at whose profile" is itself a compliance question.
    await log_action(
        db, user,
        action="profile.read",
        resource="profile",
        request=request,
        metadata={"profile_id": str(profile.id)},
    )
    return out


@router.patch("/{profile_id}", response_model=ProfileOut)
async def update_profile(
    profile_id: UUID,
    body: ProfileUpdate,
    request: Request,
    user: User = Depends(_ADMIN_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Partial update. ``model_dump(exclude_unset=True)`` so the admin
    can clear a field by sending explicit null while also being able
    to update one-field-at-a-time without touching others.
    """
    profile = await _get_profile_or_404(profile_id, db)

    patch = body.model_dump(exclude_unset=True)
    # If email changes, re-check uniqueness against the new value.
    if "email" in patch and patch["email"] != profile.email:
        dupe = (await db.execute(
            select(Profile).where(
                func.lower(Profile.email) == patch["email"].lower(),
                Profile.id != profile.id,
            )
        )).scalar_one_or_none()
        if dupe:
            raise HTTPException(
                status_code=409,
                detail=f"A profile with email {patch['email']!r} already exists.",
            )

    for field, value in patch.items():
        setattr(profile, field, value)

    await db.commit()
    await db.refresh(profile)

    await log_action(
        db, user,
        action="profile.update",
        resource="profile",
        request=request,
        # Log the FIELD NAMES that changed, never the values. A PAN
        # edit should be visible as "PAN updated" but the PAN itself
        # must not land in the audit metadata.
        metadata={"profile_id": str(profile.id), "fields": list(patch.keys())},
    )
    return ProfileOut.model_validate(profile)


@router.delete("/{profile_id}", status_code=204)
async def archive_profile(
    profile_id: UUID,
    request: Request,
    hard: bool = Query(
        default=False,
        description=(
            "If true, hard-delete the profile + every attached document "
            "(unlinks the on-disk files AND removes the rows). Intended "
            "for GDPR / DPDP erasure requests only. Default soft-delete "
            "(``archived_at`` timestamp) is reversible."
        ),
    ),
    confirm: str | None = Query(
        default=None,
        description=(
            "Required when ``hard=true`` — must exactly match the "
            "profile's email (case-insensitive). Acts as a typed "
            "second factor against accidental wipe."
        ),
        max_length=320,
    ),
    user: User = Depends(_ADMIN_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Remove a profile.

    Default ``hard=false`` — soft-delete via ``archived_at`` timestamp
    on the profile AND every attached document. Reversible by an admin
    via direct DB edit; the records remain queryable for audit
    obligations.

    F260 regression fix (feedback 5787fe53 — "the profile cannot be
    deleted, as this option is not currently available"): adds a
    ``?hard=true&confirm=<email>`` mode that physically removes the
    profile + every attached document row + every on-disk file. Mirror
    of the document-level hard-delete pattern shipped in F238(d) so
    the operator gets the same "type the email" second-factor against
    accidental clicks. No partial deletes — either everything goes or
    nothing does (a single transaction).
    """
    profile = await _get_profile_or_404(
        profile_id, db, include_archived=True
    )
    from datetime import datetime, timezone

    if hard:
        # Second-factor: typed email match. Defeats accidental clicks.
        # Comparison is case-insensitive on both sides since the
        # profile email is stored as entered.
        expected = (profile.email or "").strip().lower()
        got = (confirm or "").strip().lower()
        if not got or got != expected:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Hard-delete requires ?confirm=<profile email>. "
                    "The confirm value must exactly match the profile's "
                    "email on file."
                ),
            )

        # Unlink every attached doc's file from disk before the row
        # cascades. ``unlink_if_exists`` returns the count for audit;
        # we sum them so the audit metadata reflects what was wiped.
        docs = (await db.execute(
            select(ProfileDocument).where(ProfileDocument.profile_id == profile.id)
        )).scalars().all()
        unlinked = 0
        for d in docs:
            if d.storage_path and unlink_if_exists(d.storage_path):
                unlinked += 1

        # Cascade-delete via the FK relationship — all ProfileDocument
        # rows for this profile go with it.
        await db.delete(profile)
        await db.commit()
        await log_action(
            db, user,
            action="profile.hard_delete",
            resource="profile",
            request=request,
            metadata={
                "profile_id": str(profile_id),
                "docs_count": len(docs),
                "files_unlinked": unlinked,
            },
        )
        return Response(status_code=204)

    # ── Soft-delete (default) ───────────────────────────────────────
    if profile.archived_at is not None:
        # Already archived — idempotent no-op rather than 400. Lets
        # the UI fire delete on a row in the "archived" view without
        # extra checks.
        return Response(status_code=204)

    now = datetime.now(timezone.utc)
    profile.archived_at = now
    # Cascade the archive to docs so the list endpoint filters them
    # out consistently. Bulk update is fine — per-profile count is
    # small.
    docs = (await db.execute(
        select(ProfileDocument).where(
            ProfileDocument.profile_id == profile.id,
            ProfileDocument.archived_at.is_(None),
        )
    )).scalars().all()
    for d in docs:
        d.archived_at = now

    await db.commit()
    await log_action(
        db, user,
        action="profile.archive",
        resource="profile",
        request=request,
        metadata={"profile_id": str(profile.id), "archived_docs": len(docs)},
    )
    return Response(status_code=204)


# ── Document upload / download / delete ────────────────────────────


@router.post("/{profile_id}/documents", response_model=DocumentUploadResult, status_code=201)
async def upload_document(
    profile_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    doc_type: DocType = Form(...),
    # F241(a): cap ``doc_label`` to match the ``String(200)`` backing
    # column. Pre-fix, the Form binding accepted any-length string; a
    # 201+ char label crashed on the SQLAlchemy assignment and escaped
    # as a bare HTTP 500 ("Internal Server Error" plain-text body,
    # same shape as F239's pre-fix crash). FastAPI forwards
    # ``max_length`` to the Pydantic validator, so overflow now 422s
    # at parse time with ``string_too_long`` — actionable for the
    # admin, no DB round-trip. No sibling ``Form(default=…)`` exists
    # under ``app/api/v1/`` (verified by grep during the F241 sweep),
    # so this is a spot fix rather than a pattern-wide refactor.
    doc_label: str = Form(default="", max_length=200),
    replace_existing: bool = Form(
        default=False,
        description=(
            "If true and a non-archived doc of the same doc_type "
            "already exists on this profile, soft-archive the old "
            "one before storing the new upload. F240(b) opt-in guard "
            "against accidental duplicate uploads."
        ),
    ),
    user: User = Depends(_ADMIN_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Upload a KYC document to a profile.

    Validation pipeline:
      1. Profile must exist and not be archived (404 otherwise).
      2. ``doc_type`` is Pydantic-Literal-validated at parse time (422
         on typo).
      3. File is size+MIME+magic-byte checked by
         ``validate_and_store`` (400 on failure).
      4. ``doc_type="other"`` requires a non-empty ``doc_label`` so
         the admin can identify the file later.
      5. F240(b): an existing non-archived doc of the same ``doc_type``
         on the same profile 409s unless ``replace_existing=true``,
         in which case the previous one gets soft-archived first.
         ``doc_type="other"`` is exempt from the dedup check because
         multiple "other" docs (e.g. two different passports, voter
         ID + ration card) are legitimate and disambiguated by
         ``doc_label``.

    On success, the file lives at
    ``{PROFILE_DOC_ROOT}/{profile_id}/{doc_id}.{ext}`` with mode 0600.
    """
    profile = await _get_profile_or_404(profile_id, db)

    if doc_type == "other" and not doc_label.strip():
        raise HTTPException(
            status_code=400,
            detail='doc_type="other" requires a non-empty doc_label '
                   "describing the file",
        )

    # F240(b) duplicate-doc-type guard. The "slot" mental model in the
    # admin UI (one Aadhaar, one PAN, one 12th marksheet, …) lined up
    # with what admins actually do in practice — but nothing on the
    # backend enforced it, so a repeated click on the upload button
    # silently accumulated rows. Now:
    #   * ``doc_type="other"``  → skip the check (multi is legitimate)
    #   * ``replace_existing``  → soft-archive the prior row, proceed
    #   * otherwise             → 409 pointing at the existing row id
    if doc_type != "other":
        existing_doc = (await db.execute(
            select(ProfileDocument).where(
                ProfileDocument.profile_id == profile.id,
                ProfileDocument.doc_type == doc_type,
                ProfileDocument.archived_at.is_(None),
            )
        )).scalar_one_or_none()
        if existing_doc is not None:
            if not replace_existing:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"A non-archived '{doc_type}' document already "
                        "exists for this profile. Pass "
                        "replace_existing=true to archive the current "
                        "one and upload a replacement, or archive it "
                        "manually first."
                    ),
                )
            # Soft-archive so the audit trail keeps the previous upload.
            from datetime import datetime, timezone
            existing_doc.archived_at = datetime.now(timezone.utc)
            await db.flush()

    file_bytes = await file.read()
    doc_id = uuid.uuid4()

    # F242(b) defence-in-depth: cap the raw filename at the handler
    # boundary, BEFORE any downstream consumer touches it. F240(c)
    # already caps the value that reaches the DB column (200 ≤ 500), but
    # the tester's round-4 sweep claimed an empirical 501-char filename
    # still produced HTTP 500 against the deployed backend even after
    # the [:200] cap landed. We couldn't repro the crash locally with
    # FastAPI/Starlette/python-multipart's UploadFile path, so the most
    # likely surface is something further down (an nginx max-header
    # rule, a logging middleware that persists request metadata, a
    # response-render path that round-trips the original filename).
    # Slicing here means EVERY downstream consumer — including
    # ``validate_and_store``'s ``original_filename`` parameter and the
    # response-model serialisation — sees a value that fits in the
    # ``String(500)`` column. Single source of truth, no further
    # surprises possible. 200 chars is the same limit the download
    # handler's ``safe_name`` already imposes, so display semantics
    # don't change.
    safe_upload_filename = (file.filename or "")[:200] or None

    try:
        storage_meta = validate_and_store(
            profile_id=profile.id,
            doc_id=doc_id,
            file_bytes=file_bytes,
            content_type=file.content_type,
            original_filename=safe_upload_filename,
        )
    except DocValidationError as exc:
        # Validation failure — 400 with the error message (safe to
        # show the admin, no internal paths leaked).
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        # Filesystem failure — log with exc_info and surface a 500
        # that doesn't leak path info to the client.
        logger.exception(
            "Profile-doc filesystem write failed (profile=%s doc=%s): %s",
            profile.id, doc_id, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Document storage is temporarily unavailable. Try again later.",
        )

    # F240(c) + F242(b): use the already-capped ``safe_upload_filename``
    # from the top of the handler (200-char cap applied BEFORE the
    # validate_and_store call). Falling back to the UUID-derived default
    # when the multipart had no filename at all.
    safe_filename = safe_upload_filename or f"{doc_id}.{storage_meta['file_type']}"

    doc = ProfileDocument(
        id=doc_id,
        profile_id=profile.id,
        doc_type=doc_type,
        doc_label=(doc_label.strip() or doc_type.replace("_", " ").title()),
        filename=safe_filename,
        file_type=storage_meta["file_type"],
        storage_path=storage_meta["storage_path"],
        size_bytes=storage_meta["size_bytes"],
        checksum_sha256=storage_meta["checksum_sha256"],
        uploaded_by_user_id=user.id,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    await log_action(
        db, user,
        action="profile.document.upload",
        resource="profile_document",
        request=request,
        metadata={
            "profile_id": str(profile.id),
            "doc_id": str(doc.id),
            "doc_type": doc_type,
            "size_bytes": storage_meta["size_bytes"],
            "checksum_sha256": storage_meta["checksum_sha256"],
        },
    )
    return DocumentUploadResult(
        document=ProfileDocumentOut.model_validate(doc),
        profile_id=profile.id,
    )


@router.get("/{profile_id}/documents/{doc_id}/download")
async def download_document(
    profile_id: UUID,
    doc_id: UUID,
    request: Request,
    user: User = Depends(_ADMIN_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Stream the document bytes. ``Content-Disposition: attachment``
    with the original filename so the browser downloads rather than
    previewing in-tab (useful for HEIC etc. that wouldn't render
    anyway). Audit event fires before the read so even a filesystem
    failure after audit still leaves a record that access was attempted.
    """
    doc = (await db.execute(
        select(ProfileDocument).where(
            ProfileDocument.id == doc_id,
            ProfileDocument.profile_id == profile_id,
        )
    )).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.archived_at:
        # Archived docs stay downloadable for compliance reads — a
        # retention sweep can hard-delete them when policy fires.
        pass
    if not doc.storage_path:
        raise HTTPException(status_code=404, detail="Document has no stored file")

    # Audit BEFORE read. Even if the read fails, we want a record
    # that access was attempted.
    await log_action(
        db, user,
        action="profile.document.download",
        resource="profile_document",
        request=request,
        metadata={
            "profile_id": str(profile_id),
            "doc_id": str(doc.id),
            "doc_type": doc.doc_type,
        },
    )

    try:
        data = read_bytes(doc.storage_path)
    except (FileNotFoundError, DocValidationError) as exc:
        # Missing file (filesystem corruption, manual deletion, or
        # a tampered storage_path). Return 410 Gone so the client can
        # distinguish "row exists but bytes are missing" from 404.
        logger.warning(
            "Profile-doc read failed (doc=%s path=%s): %s",
            doc.id, doc.storage_path, exc,
        )
        raise HTTPException(
            status_code=410,
            detail="Document file is no longer available.",
        )

    # Map canonical file_type back to a Content-Type so the browser
    # does something sensible with it (thumbnail-preview for images,
    # "open with" for PDFs).
    content_type = {
        "pdf":  "application/pdf",
        "jpg":  "image/jpeg",
        "png":  "image/png",
        "heic": "image/heic",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(doc.file_type, "application/octet-stream")

    # `filename` in the disposition uses the ORIGINAL upload filename,
    # not the UUID — admin downloads "aadhaar-scan.pdf", not
    # "83f4c-.pdf". Sanitised via `filter_chars` equivalent inline.
    safe_name = "".join(
        c if c.isalnum() or c in "-._ " else "_" for c in (doc.filename or f"doc.{doc.file_type}")
    )[:200]

    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            # Explicitly forbid caching — KYC bytes should never sit in
            # an intermediate cache.
            "Cache-Control": "no-store, max-age=0",
        },
    )


@router.delete("/{profile_id}/documents/{doc_id}", status_code=204)
async def archive_document(
    profile_id: UUID,
    doc_id: UUID,
    request: Request,
    hard: bool = Query(default=False, description="If true, unlink the underlying file AND delete the row. Otherwise soft-archive."),
    confirm: str | None = Query(
        default=None,
        description=(
            "Required when ``hard=true`` — must exactly match the "
            "owning profile's email (case-insensitive). Acts as a "
            "typed second factor against accidental bulk-button misfire."
        ),
        max_length=320,
    ),
    user: User = Depends(_ADMIN_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Remove a document.

    Default soft-delete (``archived_at`` timestamp). ``?hard=true``
    physically unlinks the file and hard-deletes the row — intended
    for GDPR / DPDP erasure requests only. Hard-delete additionally
    requires ``?confirm=<email>`` matching the owning profile's email
    as a typed second factor (F238(d) regression fix).
    """
    doc = (await db.execute(
        select(ProfileDocument).where(
            ProfileDocument.id == doc_id,
            ProfileDocument.profile_id == profile_id,
        )
    )).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    from datetime import datetime, timezone
    metadata_base = {
        "profile_id": str(profile_id),
        "doc_id": str(doc.id),
        "doc_type": doc.doc_type,
    }

    if hard:
        # F238(d) regression fix — second-factor confirmation guard.
        # Hard-delete is irreversible (the file on disk gets unlinked
        # and the row is physically removed), so we require the caller
        # to explicitly re-type the owning profile's email. This turns
        # "accidental click on a delete button" into an impossible
        # mistake: a stray DELETE with just ``?hard=true`` now 400s.
        # Comparison is case-insensitive on both sides since the
        # profile email is stored as entered.
        profile = await _get_profile_or_404(
            profile_id, db, include_archived=True
        )
        expected = (profile.email or "").strip().lower()
        got = (confirm or "").strip().lower()
        if not got or got != expected:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Hard-delete requires ?confirm=<owning profile email>. "
                    "The confirm value must exactly match the profile's "
                    "email on file."
                ),
            )
        unlinked = False
        if doc.storage_path:
            unlinked = unlink_if_exists(doc.storage_path)
        await db.delete(doc)
        await db.commit()
        await log_action(
            db, user,
            action="profile.document.hard_delete",
            resource="profile_document",
            request=request,
            metadata={**metadata_base, "file_unlinked": unlinked},
        )
    else:
        doc.archived_at = datetime.now(timezone.utc)
        await db.commit()
        await log_action(
            db, user,
            action="profile.document.archive",
            resource="profile_document",
            request=request,
            metadata=metadata_base,
        )
    return Response(status_code=204)
