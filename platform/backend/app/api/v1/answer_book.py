"""Answer book management endpoints.

Regression finding 80: `POST /answer-book` and `PATCH /answer-book/{id}`
previously declared `body: dict`. Replaced with explicit Pydantic
`AnswerCreate` / `AnswerUpdate` schemas which cap `question` at 2 KB
and `answer` at 8 KB (matching the `_LONG_TEXT_MAX` used elsewhere),
enforce the category allowlist at parse time, and remove `source`
from the input surface entirely — the server always sets it based on
the endpoint, eliminating the provenance-spoofing footgun.
"""

import logging
import re
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import func

logger = logging.getLogger(__name__)

from app.database import get_db
from app.models.answer_book import AnswerBookEntry
from app.models.resume import Resume
from app.models.user import User
from app.api.deps import get_current_user
from app.schemas.answer_book import AnswerCreate, AnswerUpdate
from app.schemas.routine import (
    RequiredCoverageEntry,
    RequiredCoverageResponse,
    SeedRequiredResponse,
)
from app.services.answer_book_seed import REQUIRED_ENTRIES, REQUIRED_QUESTION_KEYS

router = APIRouter(prefix="/answer-book", tags=["answer-book"])

VALID_CATEGORIES = ["personal_info", "work_auth", "experience", "skills", "preferences", "custom"]


def normalize_question_key(question: str) -> str:
    """Normalize a question into a key for matching."""
    key = question.lower().strip()
    key = re.sub(r"[^\w\s]", "", key)
    key = re.sub(r"\s+", "_", key)
    return key[:255]


def _extract_fields_from_resume_text(text: str) -> list[tuple[str, str, str]]:
    """Regex-extract personal-info fields from resume text.

    Returns a list of ``(question, answer, category)`` tuples for the
    fields found in ``text``. Pure function — no DB or session touch —
    so it's safe to unit-test and to call from sync or async contexts.

    Kept here rather than in ``utils/`` because the question wording
    must match what the persist step uses when it computes
    ``question_key`` — co-locating avoids the drift bug where the
    extractor and the persist step disagree on key normalization.
    """
    extracted: list[tuple[str, str, str]] = []

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    if email_match:
        extracted.append(("What is your email address?", email_match.group(), "personal_info"))

    phone_match = re.search(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    if phone_match:
        extracted.append(("What is your phone number?", phone_match.group(), "personal_info"))

    linkedin_match = re.search(r"linkedin\.com/in/[\w-]+", text, re.IGNORECASE)
    if linkedin_match:
        extracted.append(("What is your LinkedIn URL?", f"https://{linkedin_match.group()}", "personal_info"))

    github_match = re.search(r"github\.com/[\w-]+", text, re.IGNORECASE)
    if github_match:
        extracted.append(("What is your GitHub URL?", f"https://{github_match.group()}", "personal_info"))

    return extracted


async def auto_populate_from_resume(
    db: AsyncSession,
    user_id: uuid.UUID,
    resume: Resume,
) -> dict:
    """Extract personal-info fields from a resume and upsert them as
    base (non-resume-scoped) answer-book entries for ``user_id``.

    Called from three places:
      * Resume upload (``resume.py:upload_resume``) — auto-populates on
        first ingest so a brand-new user doesn't have to click a button.
      * Resume switch (``resume.py:switch_active_resume``) — backfills
        any fields the previous active resume didn't have.
      * The optional ``/answer-book/import-from-resume`` manual endpoint
        (kept for callers that pre-date the auto-hook).

    Upsert semantics: for each extracted field, we check for an existing
    base entry (resume_id IS NULL) with the same normalized question_key.
    Presence wins — we don't overwrite an answer the user may have edited
    by hand. Only adds rows; never updates or deletes. Idempotent: calling
    twice on the same resume is a no-op on the second call.

    Does NOT commit — the caller's transaction owns the commit lifecycle,
    so auto-populate can share a commit with resume upload. ``db.flush()``
    happens inside to propagate FK constraints, but the final commit is
    the caller's.

    Returns ``{"extracted": N, "added": M, "fields": [...]}`` — matches
    the legacy manual-endpoint response shape so existing callers don't
    break.
    """
    text = resume.text_content or ""
    extracted = _extract_fields_from_resume_text(text)

    added = 0
    for question, answer, category in extracted:
        question_key = normalize_question_key(question)
        existing = (await db.execute(
            select(AnswerBookEntry).where(
                AnswerBookEntry.user_id == user_id,
                AnswerBookEntry.question_key == question_key,
                AnswerBookEntry.resume_id.is_(None),
            )
        )).scalar_one_or_none()

        if existing:
            continue

        db.add(AnswerBookEntry(
            id=uuid.uuid4(),
            user_id=user_id,
            resume_id=None,
            category=category,
            question=question,
            question_key=question_key,
            answer=answer,
            source="resume_extracted",
        ))
        added += 1

    # Flush so any immediate reads in the same transaction see the new
    # rows. Commit is intentionally the caller's responsibility.
    if added:
        await db.flush()

    return {
        "extracted": len(extracted),
        "added": added,
        "fields": [{"question": q, "answer": a} for q, a, _ in extracted],
    }


@router.get("")
async def get_answer_book(
    category: str | None = Query(default=None),
    resume_id: UUID | None = Query(default=None),
    q: str | None = Query(
        default=None,
        max_length=200,
        description="Case-insensitive substring filter over question + answer.",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get merged answer book entries (base + resume overrides).

    Regression finding 172: `resume_id` was previously silently dropped
    by FastAPI (the handler never declared it), so the frontend's
    `?resume_id=X` calls returned the same response as the no-param
    call — always merging base + user.active_resume_id. When a user
    wanted to review overrides for a non-active resume (e.g. while
    switching resumes, or in the resume-comparison flow), they had no
    way to get them.

    F236 regression fix: add the canonical pagination envelope
    (``{items, total, page, page_size, total_pages}``) plus a free-
    text ``q`` filter to match the shape used by ``/feedback``,
    ``/jobs``, ``/applications``. ``categories`` and ``active_resume_id``
    stay at the top level for backward compatibility with the
    AnswerBookPage UI — both fields are envelope-sidecar, not
    per-page metadata.

    Contract:
      - ``resume_id`` omitted  → base + user.active_resume_id overrides
        (unchanged, backward-compatible default)
      - ``resume_id`` provided → base + that resume's overrides, after
        verifying the caller owns the resume (404 otherwise — match
        the existing pattern on `create_answer`)
      - ``category`` and ``q`` filters run BEFORE the resume-override
        merge; pagination happens AFTER, counting merged rows — so a
        base-vs-override pair with the same ``question_key`` counts as
        one item for ``total``.
    """
    # Resolve the effective override resume. None ⇒ "no overrides".
    override_resume_id = None
    if resume_id is not None:
        resume = (await db.execute(
            select(Resume.id).where(Resume.id == resume_id, Resume.user_id == user.id)
        )).scalar_one_or_none()
        if not resume:
            raise HTTPException(status_code=404, detail="Resume not found")
        override_resume_id = resume_id
    elif user.active_resume_id:
        override_resume_id = user.active_resume_id

    query = select(AnswerBookEntry).where(
        AnswerBookEntry.user_id == user.id,
        or_(
            AnswerBookEntry.resume_id.is_(None),
            AnswerBookEntry.resume_id == override_resume_id,
        ) if override_resume_id else AnswerBookEntry.resume_id.is_(None),
    )
    if category:
        query = query.where(AnswerBookEntry.category == category)
    if q and q.strip():
        # Case-insensitive substring over question + answer. The
        # frontend just needs "find anything that mentions X" behaviour
        # — no regex/full-text yet. If this grows we can wire a
        # tsvector index later, but for the typical ~50-entry personal
        # answer book a LIKE scan is trivially fast.
        needle = f"%{q.strip().lower()}%"
        query = query.where(
            or_(
                func.lower(AnswerBookEntry.question).like(needle),
                func.lower(AnswerBookEntry.answer).like(needle),
            )
        )
    query = query.order_by(AnswerBookEntry.category, AnswerBookEntry.question)

    result = await db.execute(query)
    entries = result.scalars().all()

    # Merge: resume-specific overrides win on matching question_key.
    # Done in Python because the merge rule is business-logic and the
    # answer book is small (~tens of rows), so a SQL-level override
    # join would be more code than it's worth.
    merged: dict[str, AnswerBookEntry] = {}
    for entry in entries:
        key = entry.question_key
        if key not in merged or entry.resume_id is not None:
            merged[key] = entry

    # Stable ordering across the merged dict: SQL-level order_by is by
    # (category, question) ascending. Python dicts preserve insertion
    # order as of 3.7 — first write wins the position, later override
    # just replaces the value at that key. So merged.values() is
    # already in the intended order.
    all_items = list(merged.values())
    total = len(all_items)
    total_pages = (total + page_size - 1) // page_size if total else 0
    start = (page - 1) * page_size
    end = start + page_size
    window = all_items[start:end]

    return {
        "items": [
            {
                "id": str(e.id),
                "user_id": str(e.user_id),
                "resume_id": str(e.resume_id) if e.resume_id else None,
                "category": e.category,
                "question": e.question,
                "question_key": e.question_key,
                "answer": e.answer,
                "source": e.source,
                "is_override": e.resume_id is not None,
                "usage_count": e.usage_count,
                "created_at": e.created_at.isoformat(),
                "updated_at": e.updated_at.isoformat(),
            }
            for e in window
        ],
        # Canonical pagination envelope — mirrors /feedback, /jobs.
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        # Backward-compat sidecars. Not per-page state.
        "categories": VALID_CATEGORIES,
        "active_resume_id": str(user.active_resume_id) if user.active_resume_id else None,
    }


@router.post("")
async def create_answer(
    body: AnswerCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an answer book entry."""
    # AnswerCreate has already validated: category is in the allowlist,
    # question is 1-2000 chars, answer is 0-8000 chars. Any violation
    # returned 422 before we got here.
    question = body.question.strip()
    answer = body.answer.strip()
    category = body.category
    resume_id = body.resume_id  # None for base entry

    # Verify resume ownership if resume_id provided
    if resume_id:
        resume = (await db.execute(
            select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
        )).scalar_one_or_none()
        if not resume:
            raise HTTPException(status_code=404, detail="Resume not found")

    question_key = normalize_question_key(question)

    # Reject attempts to create routine-reserved keys via the generic
    # POST path. These must be seeded through /answer-book/seed-required
    # so the is_locked=True flag is set correctly; letting a caller
    # create an unlocked row with one of these keys would break the
    # lock contract.
    if question_key in REQUIRED_QUESTION_KEYS:
        raise HTTPException(
            status_code=400,
            detail="This question is reserved for routine setup. "
                   "Use POST /answer-book/seed-required to create, "
                   "then PATCH the answer via /answer-book/{id}.",
        )

    # Check for duplicate
    dup_query = select(AnswerBookEntry).where(
        AnswerBookEntry.user_id == user.id,
        AnswerBookEntry.question_key == question_key,
    )
    if resume_id:
        dup_query = dup_query.where(AnswerBookEntry.resume_id == resume_id)
    else:
        dup_query = dup_query.where(AnswerBookEntry.resume_id.is_(None))

    existing = (await db.execute(dup_query)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="An entry with this question already exists")

    entry = AnswerBookEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        resume_id=resume_id,
        category=category,
        question=question,
        question_key=question_key,
        answer=answer,
        # `source` is server-controlled — removed from the input schema
        # to close the provenance-spoofing footgun. Import-from-resume
        # sets `"resume_extracted"`, DELETE soft-archive sets
        # `"archived"`; user-created entries are always `"manual"`.
        source="manual",
    )
    db.add(entry)

    # F322 (race-safe): the lookup-then-INSERT above is TOCTOU-
    # vulnerable to concurrent POSTs from the same user (two
    # browser tabs, double-click, etc.) — both pass the dup
    # check, both reach INSERT, the second hits
    # ``uq_answer_user_resume_key`` and 500s. Wrap in SAVEPOINT
    # and surface the constraint violation as the same 409 the
    # handler-check returns.
    from sqlalchemy.exc import IntegrityError
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An entry with this question already exists",
        )

    await db.commit()
    await db.refresh(entry)

    return {
        "id": str(entry.id),
        "question": entry.question,
        "question_key": entry.question_key,
        "answer": entry.answer,
        "category": entry.category,
        "resume_id": str(entry.resume_id) if entry.resume_id else None,
    }


@router.patch("/{entry_id}")
async def update_answer(
    entry_id: UUID,
    body: AnswerUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update an answer book entry."""
    entry = (await db.execute(
        select(AnswerBookEntry).where(
            AnswerBookEntry.id == entry_id,
            AnswerBookEntry.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    # AnswerUpdate already validated each supplied field; fields left
    # unset (None) mean "don't touch". We use `model_fields_set` to
    # distinguish an explicit `null` (which we still treat as no-op for
    # these non-nullable DB columns) from omission.
    fields = body.model_fields_set

    # Lock enforcement: routine-reserved entries (is_locked=True) allow
    # the ANSWER field to be updated — that's the point of the /required-
    # setup flow — but the question text and category are frozen. This
    # is a hard 400 rather than a silent skip so a caller who typo's
    # the entry_id can't accidentally mutate the seeded question.
    if entry.is_locked:
        if "question" in fields and body.question is not None and body.question != entry.question:
            raise HTTPException(
                status_code=400,
                detail="This entry is locked (routine setup). "
                       "Only the answer may be updated.",
            )
        if "category" in fields and body.category is not None and body.category != entry.category:
            raise HTTPException(
                status_code=400,
                detail="This entry is locked (routine setup). "
                       "Only the answer may be updated.",
            )

    if "answer" in fields and body.answer is not None:
        entry.answer = body.answer
    if "question" in fields and body.question is not None:
        entry.question = body.question
        entry.question_key = normalize_question_key(body.question)
    if "category" in fields and body.category is not None:
        entry.category = body.category

    db.add(entry)
    await db.commit()

    return {"id": str(entry.id), "answer": entry.answer, "question": entry.question}


@router.delete("/{entry_id}")
async def archive_answer(
    entry_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Archive an answer book entry (soft-delete — data is preserved)."""
    entry = (await db.execute(
        select(AnswerBookEntry).where(
            AnswerBookEntry.id == entry_id,
            AnswerBookEntry.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    # Locked (routine-reserved) entries can't be archived — they're
    # permanent slots. If the user wants to clear their answer they
    # can PATCH it to empty string, and the pre-flight coverage check
    # will flag the entry as unfilled again.
    if entry.is_locked:
        raise HTTPException(
            status_code=400,
            detail="This entry is locked (routine setup) and cannot be deleted. "
                   "PATCH the answer to clear it instead.",
        )

    entry.source = "archived"
    await db.commit()
    return {"status": "archived", "message": "Entry archived (data preserved)"}


@router.post("/import-from-resume/{resume_id}")
async def import_from_resume(
    resume_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Extract personal info from resume text into answer book base entries.

    Note: answer-book population now runs automatically on resume upload
    and on active-resume switch (see ``resume.py``), so the frontend's
    "Import from Resume" button is gone and this endpoint is rarely
    called directly. Retained for:

    * Re-populating after the user deleted one of the extracted entries
      and wants it back from the resume.
    * Scripting / API clients that pre-date the auto-hook.

    The logic itself lives in :func:`auto_populate_from_resume` so the
    extraction + upsert rules are identical across all three call sites.
    """
    resume = (await db.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id)
    )).scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    result = await auto_populate_from_resume(db, user.id, resume)
    await db.commit()
    return result


@router.get("/coverage")
async def get_coverage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get answer book coverage stats by category and top used entries."""
    # Fetch all entries (base + active resume)
    query = select(AnswerBookEntry).where(
        AnswerBookEntry.user_id == user.id,
        or_(
            AnswerBookEntry.resume_id.is_(None),
            AnswerBookEntry.resume_id == user.active_resume_id,
        ) if user.active_resume_id else AnswerBookEntry.resume_id.is_(None),
    )
    result = await db.execute(query)
    entries = result.scalars().all()

    # Merge (resume-specific wins)
    merged: dict[str, AnswerBookEntry] = {}
    for entry in entries:
        key = entry.question_key
        if key not in merged or entry.resume_id is not None:
            merged[key] = entry

    # Category coverage
    categories: dict[str, dict] = {}
    for cat in VALID_CATEGORIES:
        categories[cat] = {"count": 0, "with_answer": 0}

    for e in merged.values():
        cat = e.category or "custom"
        if cat in categories:
            categories[cat]["count"] += 1
            if e.answer and e.answer.strip():
                categories[cat]["with_answer"] += 1

    # Top used entries
    top_used = sorted(
        [e for e in merged.values() if e.usage_count > 0],
        key=lambda e: e.usage_count,
        reverse=True,
    )[:10]

    return {
        "total_entries": len(merged),
        "categories": categories,
        "top_used": [
            {
                "question_key": e.question_key,
                "question": e.question,
                "usage_count": e.usage_count,
                "category": e.category,
            }
            for e in top_used
        ],
    }


# ═══════════════════════════════════════════════════════════════════
# Claude Routine Apply — required-setup endpoints
# ═══════════════════════════════════════════════════════════════════

async def _seed_required_for_user(
    user: User,
    db: AsyncSession,
) -> tuple[int, int]:
    """Idempotently create any missing routine-required entries.

    Returns ``(created, already_present)``. Extracted from the POST
    handler so the GET ``/required-coverage`` endpoint can call it
    inline (F256 fix — see the GET handler for why).

    Every row is created with ``source="manual_required"``,
    ``is_locked=True``, and ``answer=""``. The user fills in answers
    via PATCH /answer-book/{id} on the same page.
    """
    created = 0
    already_present = 0
    for category, question_key, question in REQUIRED_ENTRIES:
        # Uniqueness key matches the DB constraint (user_id, resume_id
        # IS NULL, question_key). We always seed as shared entries
        # (resume_id=None) because the routine reads them regardless
        # of which resume is active.
        existing = (await db.execute(
            select(AnswerBookEntry).where(
                AnswerBookEntry.user_id == user.id,
                AnswerBookEntry.resume_id.is_(None),
                AnswerBookEntry.question_key == question_key,
            )
        )).scalar_one_or_none()

        if existing:
            # If an older unlocked row exists with this key (e.g. from
            # resume-extracted auto-population before this feature
            # shipped), upgrade it in place to locked+manual_required.
            # The user's previously-typed answer is preserved.
            if not existing.is_locked or existing.source != "manual_required":
                existing.is_locked = True
                existing.source = "manual_required"
                existing.category = category
                existing.question = question
                db.add(existing)
            already_present += 1
            continue

        db.add(AnswerBookEntry(
            id=uuid.uuid4(),
            user_id=user.id,
            resume_id=None,
            category=category,
            question=question,
            question_key=question_key,
            answer="",
            source="manual_required",
            is_locked=True,
        ))
        created += 1
    if created or already_present:
        await db.commit()
    return created, already_present


@router.post("/seed-required", response_model=SeedRequiredResponse)
async def seed_required(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create the 16 routine-required answer-book entries for this user.

    Idempotent: entries that already exist for ``(user_id,
    question_key)`` are skipped (counted in ``already_present``).
    Safe to re-call at any time. As of F256 the seed also runs
    automatically on first ``GET /required-coverage`` so the user
    never has to click this button — it stays around for manual
    re-seed after data resets.
    """
    created, already_present = await _seed_required_for_user(user, db)
    return SeedRequiredResponse(
        created=created,
        already_present=already_present,
        total=len(REQUIRED_ENTRIES),
    )


@router.get("/required-coverage", response_model=RequiredCoverageResponse)
async def required_coverage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Report which routine-required entries are filled.

    The routine's pre-flight gate: if ``complete=False``, it refuses
    to run any application until every entry has a non-empty answer.
    The UI /required-setup page shows this as a progress bar +
    list of unfilled entries.

    F256 regression fix: this endpoint now AUTO-SEEDS missing entries
    on read. Pre-fix the unseeded entries were returned with
    placeholder ``uuid.uuid4()`` ids freshly generated on every GET.
    Those ids didn't exist in the DB, so the user typing into the
    input + clicking Save fired ``PATCH /answer-book/{placeholder}``
    → 404 → save mutation failed silently → "values are not saving"
    user report (and "saved values not visible" for the same reason —
    nothing was ever persisted). Auto-seed makes every returned id
    real-and-PATCH-able from the very first page load. The seed is
    idempotent so repeated reads cost only the SELECT scan when
    everything's already present.

    Returns rows ordered by the canonical seed order so the UI
    renders them in a predictable sequence (comp, work-auth, EEO).
    """
    # F256: ensure every required entry exists before computing
    # coverage. ``_seed_required_for_user`` is idempotent and a no-op
    # commit when nothing's missing.
    await _seed_required_for_user(user, db)

    result = await db.execute(
        select(AnswerBookEntry).where(
            AnswerBookEntry.user_id == user.id,
            AnswerBookEntry.is_locked == True,  # noqa: E712  SQLAlchemy equality
            AnswerBookEntry.source == "manual_required",
        )
    )
    entries = {e.question_key: e for e in result.scalars().all()}

    # Walk REQUIRED_ENTRIES in canonical order so the UI renders a
    # stable list — independent of DB insert order. After the
    # auto-seed above, ``entries.get(question_key)`` should always
    # return a real row; the placeholder branch is kept as
    # belt-and-suspenders against a race or partial-commit scenario.
    items: list[RequiredCoverageEntry] = []
    for category, question_key, question in REQUIRED_ENTRIES:
        entry = entries.get(question_key)
        if entry is None:
            # Auto-seed should have filled this. If we still hit it
            # the seed transaction was somehow skipped — log but don't
            # 500 the read; placeholder id keeps the UI list stable.
            logger.warning(
                "required-coverage: missing entry %s after auto-seed (user=%s)",
                question_key, user.id,
            )
            items.append(RequiredCoverageEntry(
                id=uuid.uuid4(),
                category=category,
                question=question,
                question_key=question_key,
                answer="",
                filled=False,
            ))
        else:
            filled = bool(entry.answer and entry.answer.strip())
            items.append(RequiredCoverageEntry(
                id=entry.id,
                category=entry.category,
                question=entry.question,
                question_key=entry.question_key,
                answer=entry.answer,
                filled=filled,
            ))

    total_filled = sum(1 for it in items if it.filled)
    missing = [it for it in items if not it.filled]
    return RequiredCoverageResponse(
        complete=(total_filled == len(REQUIRED_ENTRIES)),
        total_required=len(REQUIRED_ENTRIES),
        total_filled=total_filled,
        missing=missing,
        # Phase-2: expose the full list so the UI can render filled
        # rows too (and let the operator edit them in-place). ``items``
        # is already in canonical seed order from the loop above.
        entries=items,
    )
