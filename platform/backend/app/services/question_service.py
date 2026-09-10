"""Question caching and answer book auto-population service."""

import uuid
import re
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.job_question import JobQuestion
from app.models.answer_book import AnswerBookEntry

logger = logging.getLogger(__name__)


def _normalise_key(text: str) -> str:
    """Normalize text to a field key."""
    key = text.lower().strip()
    key = re.sub(r"[^\w\s]", "", key)
    key = re.sub(r"\s+", "_", key)
    return key[:255]


def _cached_row_to_dict(q: JobQuestion) -> dict:
    """Render a cached ``JobQuestion`` row as a question dict.

    Shared by the async and sync cache-read paths so the two can't
    drift — they returned subtly different shapes before F346 (the sync
    one didn't coerce ``None`` the way F182 required of the async one).

    ``extraction_mode`` is derived rather than stored: ``job_questions``
    has no such column, and the value is a pure function of the platform
    (we either have an extractor for it or we don't), so deriving on
    read is equivalent to a column plus a backfill, and free.
    """
    from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS

    return {
        # F182 (b): coerce None back to "" / [] — DB columns are
        # nominally non-null but legacy rows may carry NULLs from when
        # `default=list` wasn't honoured on insert. Cheap coercion here
        # prevents a downstream `.get()` on `None` from 500ing.
        "field_key": q.field_key or "",
        "label": q.label or "",
        "field_type": q.field_type or "text",
        "required": bool(q.required),
        "options": q.options if isinstance(q.options, list) else [],
        "description": q.description or "",
        "extraction_mode": (
            "extracted"
            if (q.platform or "") in SUPPORTED_QUESTION_PLATFORMS
            else "fallback"
        ),
    }


def _with_alternative_groups(rows: list[dict]) -> list[dict]:
    """Re-derive F350 alternative groups for cached rows.

    F363. The extractor tags fields that are alternatives for one ATS
    question (Greenhouse "Resume/CV" -> resume | resume_text) with a
    shared ``alternative_group``, and the gate treats the group as one
    requirement. But ``job_questions`` has no such column, so the tag
    survived only the first, uncached fetch. Every later view — which
    in production is nearly every view — read the rows back untagged,
    and ``resume_text`` blocked on its own even with a resume attached.
    Found on a live application whose review screen listed "Resume/CV"
    as blocking after the F362 fix should have cleared it.

    The signal is recoverable without a migration: alternatives are the
    rows of one job that share a label. A label carried by exactly one
    row stays ungrouped.
    """
    from collections import Counter

    from app.fetchers.questions import _normalise_field_key

    counts = Counter((r.get("label") or "").strip() for r in rows)
    for r in rows:
        label = (r.get("label") or "").strip()
        if label and counts[label] > 1:
            r["alternative_group"] = f"altgroup_{_normalise_field_key(label)}"
        else:
            r.setdefault("alternative_group", "")
    return rows


async def get_or_fetch_questions(db: AsyncSession, job, board_slug: str) -> list[dict]:
    """Get cached questions or fetch from ATS API, then cache.

    Regression finding 182: one specific job (Wiz SRE —
    `6f9371a8-e4f8-4166-8ed8-920682309cd4`) was reproducibly returning
    HTTP 500 on `GET /applications/questions/{id}`. Root cause wasn't
    conclusively identified because the 500 body carried no detail,
    but the most likely candidates were (a) UniqueConstraint clashes
    on re-insert when two fetched fields shared an empty `field_key`,
    (b) NULL `options` / `label` / `description` in cached rows
    triggering a downstream KeyError or `NoneType.get(...)` in the
    answer-matcher, and (c) an IntegrityError on the flush that
    poisoned the session for the subsequent `await db.commit()` in
    the handler. Defence in depth:
      - dedupe by `field_key` before insert so a malformed upstream
        response with duplicate keys can never hit the unique index.
      - coerce any cached `None` into its safe default when reading
        back (`options` → `[]`, text fields → `""`) so the matcher
        never receives `None` where it expected a value.
    """
    # Check cache first
    result = await db.execute(
        select(JobQuestion).where(JobQuestion.job_id == job.id)
    )
    cached = result.scalars().all()

    if cached:
        return _with_alternative_groups([_cached_row_to_dict(q) for q in cached])

    # Fetch from ATS API
    from app.fetchers.questions import fetch_application_questions
    questions = fetch_application_questions(job.platform, job.external_id, board_slug)

    # F182 (a): dedupe fetched questions by `field_key` so we never
    # try to insert two JobQuestion rows with the same
    # (job_id, field_key) — the `uq_job_question_field` unique index
    # would raise IntegrityError on flush, poison the session, and
    # surface as opaque 500. Also skip rows with empty `field_key`
    # since an empty key can't match anything downstream anyway and
    # two empty keys in the same fetch would collide on the index.
    seen_keys: set[str] = set()
    deduped: list[dict] = []
    for q in questions:
        fk = (q.get("field_key") or "").strip()
        if not fk or fk in seen_keys:
            continue
        seen_keys.add(fk)
        deduped.append(q)

    # F357 — only cache a REAL extraction. `extraction_mode` is derived
    # on read from the platform, so caching a fallback schema would make
    # it read back as "extracted" the next time and hand the apply gate
    # a guessed form wearing an extracted label. Not caching fallbacks
    # keeps the derivation honest without needing a migration, and costs
    # one cheap upstream call per view on platforms we can't read anyway.
    if any(q.get("extraction_mode") == "fallback" for q in questions):
        return questions

    # Cache in DB
    for q in deduped:
        jq = JobQuestion(
            id=uuid.uuid4(),
            job_id=job.id,
            field_key=q.get("field_key", ""),
            label=q.get("label", "") or "",
            field_type=q.get("field_type", "text") or "text",
            required=bool(q.get("required", False)),
            options=q.get("options") if isinstance(q.get("options"), list) else [],
            description=q.get("description", "") or "",
            platform=job.platform,
        )
        db.add(jq)

    try:
        await db.flush()
    except Exception:
        # F182 (c): on flush failure, rollback resets the session
        # but any pending writes from EARLIER in the handler also
        # vanish. The handler must be prepared for that (it only
        # writes answer-book rows AFTER this call, so the answer-book
        # commit path is unaffected — but we still log with
        # `exc_info=True` now so the underlying IntegrityError is
        # visible in logs instead of hidden behind a generic warn.
        await db.rollback()
        logger.warning("Failed to cache questions for job %s", job.id, exc_info=True)

    return questions


def get_or_fetch_questions_sync(session: Session, job, board_slug: str) -> list[dict]:
    """Sync version for Celery tasks."""
    cached = session.execute(
        select(JobQuestion).where(JobQuestion.job_id == job.id)
    ).scalars().all()

    if cached:
        return _with_alternative_groups([_cached_row_to_dict(q) for q in cached])

    from app.fetchers.questions import fetch_application_questions
    questions = fetch_application_questions(job.platform, job.external_id, board_slug)

    # F357 — same rule as the async path: never cache a guessed schema.
    if any(q.get("extraction_mode") == "fallback" for q in questions):
        return questions

    for q in questions:
        jq = JobQuestion(
            id=uuid.uuid4(),
            job_id=job.id,
            field_key=q.get("field_key", ""),
            label=q.get("label", ""),
            field_type=q.get("field_type", "text"),
            required=q.get("required", False),
            options=q.get("options", []),
            description=q.get("description", ""),
            platform=job.platform,
        )
        session.add(jq)

    try:
        session.flush()
    except Exception:
        session.rollback()
        logger.warning("Failed to cache questions for job %s", job.id)

    return questions


async def auto_populate_answer_book(db: AsyncSession, user_id, questions: list[dict]) -> int:
    """Create placeholder answer book entries for new questions.

    Skips file-type fields. Returns count of new entries created.
    """
    from app.workers.tasks._answer_prep import _FIELD_ALIASES

    # Load existing entries for this user (base only)
    result = await db.execute(
        select(AnswerBookEntry).where(
            AnswerBookEntry.user_id == user_id,
            AnswerBookEntry.resume_id.is_(None),
        )
    )
    existing = result.scalars().all()
    existing_keys = {e.question_key for e in existing}

    # Build reverse alias map: alias -> canonical key
    all_known_keys = set(existing_keys)
    for canonical, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in existing_keys or _normalise_key(alias) in existing_keys:
                all_known_keys.add(canonical)
                all_known_keys.update(_normalise_key(a) for a in aliases)
                break

    added = 0
    for q in questions:
        if q.get("field_type") == "file":
            continue

        field_key = _normalise_key(q.get("field_key", "") or q.get("label", ""))
        if not field_key:
            continue

        if field_key in all_known_keys:
            continue

        # Check aliases
        is_known = False
        for canonical, aliases in _FIELD_ALIASES.items():
            norm_aliases = [_normalise_key(a) for a in aliases]
            if field_key in norm_aliases or canonical == field_key:
                if canonical in all_known_keys or any(a in all_known_keys for a in norm_aliases):
                    is_known = True
                    break

        if is_known:
            continue

        # Guess category
        category = _guess_category(field_key)

        label = q.get("label", field_key.replace("_", " ").title())

        entry = AnswerBookEntry(
            id=uuid.uuid4(),
            user_id=user_id,
            resume_id=None,
            category=category,
            question=label,
            question_key=field_key,
            answer="",
            source="ats_discovered",
        )
        db.add(entry)
        all_known_keys.add(field_key)
        added += 1

    if added:
        try:
            await db.flush()
        except Exception:
            await db.rollback()
            logger.warning("Failed to auto-populate answer book entries")
            added = 0

    return added


def _guess_category(field_key: str) -> str:
    """Guess answer book category from field key."""
    personal = ["first_name", "last_name", "email", "phone", "name", "linkedin", "website", "github", "address", "city", "zip", "country"]
    work_auth = ["work_auth", "sponsor", "authorized", "visa", "legally", "citizenship", "eligible"]
    experience = ["experience", "cover_letter", "tell_us", "why_do_you", "achievement", "describe", "about_yourself"]
    skills = ["skills", "technologies", "languages", "proficient", "certif"]
    preferences = ["salary", "compensation", "start_date", "relocat", "schedule", "travel", "notice_period"]

    for kw in personal:
        if kw in field_key:
            return "personal_info"
    for kw in work_auth:
        if kw in field_key:
            return "work_auth"
    for kw in experience:
        if kw in field_key:
            return "experience"
    for kw in skills:
        if kw in field_key:
            return "skills"
    for kw in preferences:
        if kw in field_key:
            return "preferences"
    return "custom"
