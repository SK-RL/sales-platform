"""Interview question repository — ticket 8ef0e9c2.

CRUD + search over interview debriefs. Access model:

* Every authenticated user can READ and CREATE — the whole point is
  shared institutional memory across the sales/candidate team.
* Only the author or an admin can EDIT / DELETE a debrief.

Search is ILIKE across company / role / questions / candidate —
the corpus is human-entered (hundreds of rows), so a LIKE scan is
plenty; revisit with pg_trgm only if the table ever gets big enough
to feel it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.interview_question import InterviewQuestionSet
from app.models.user import User
from app.utils.sql import escape_like

router = APIRouter(prefix="/interview-questions", tags=["interview-questions"])


class InterviewQuestionSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1, max_length=300)
    job_role: str = Field(min_length=1, max_length=200)
    interview_round: str = Field(min_length=1, max_length=100)
    interview_date: date | None = None
    candidate_name: str = Field(default="", max_length=200)
    interviewer: str = Field(default="", max_length=300)
    # One question per line by convention; the frontend renders a
    # numbered list. 16 KB cap = ~200 long questions, far beyond any
    # real debrief, cheap enough to store.
    questions: str = Field(min_length=1, max_length=16000)
    notes: str = Field(default="", max_length=8000)


class InterviewQuestionSetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str | None = Field(default=None, min_length=1, max_length=300)
    job_role: str | None = Field(default=None, min_length=1, max_length=200)
    interview_round: str | None = Field(default=None, min_length=1, max_length=100)
    interview_date: date | None = None
    candidate_name: str | None = Field(default=None, max_length=200)
    interviewer: str | None = Field(default=None, max_length=300)
    questions: str | None = Field(default=None, min_length=1, max_length=16000)
    notes: str | None = Field(default=None, max_length=8000)


class InterviewQuestionSetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID | None
    author_name: str | None = None
    company_name: str
    job_role: str
    interview_round: str
    interview_date: date | None
    candidate_name: str
    interviewer: str
    questions: str
    notes: str
    created_at: datetime
    updated_at: datetime


def _serialize(row: InterviewQuestionSet) -> InterviewQuestionSetOut:
    out = InterviewQuestionSetOut.model_validate(row)
    # ``author`` is lazy="joined" so this is loaded already; SET NULL
    # rows (deleted author) render as None → the UI shows "—".
    out.author_name = row.author.name if row.author else None
    return out


@router.get("")
async def list_question_sets(
    q: str | None = Query(default=None, max_length=200),
    company: str | None = Query(default=None, max_length=300),
    role: str | None = Query(default=None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Searchable list, newest first. Canonical pagination envelope."""
    query = select(InterviewQuestionSet)
    if q and q.strip():
        needle = f"%{escape_like(q.strip())}%"
        query = query.where(
            or_(
                InterviewQuestionSet.company_name.ilike(needle, escape="\\"),
                InterviewQuestionSet.job_role.ilike(needle, escape="\\"),
                InterviewQuestionSet.questions.ilike(needle, escape="\\"),
                InterviewQuestionSet.candidate_name.ilike(needle, escape="\\"),
                InterviewQuestionSet.interview_round.ilike(needle, escape="\\"),
            )
        )
    if company and company.strip():
        query = query.where(
            InterviewQuestionSet.company_name.ilike(
                f"%{escape_like(company.strip())}%", escape="\\"
            )
        )
    if role and role.strip():
        query = query.where(
            InterviewQuestionSet.job_role.ilike(
                f"%{escape_like(role.strip())}%", escape="\\"
            )
        )

    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar_one()
    rows = (await db.execute(
        query.order_by(InterviewQuestionSet.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all()

    return {
        "items": [_serialize(r).model_dump() for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.post("", status_code=201, response_model=InterviewQuestionSetOut)
async def create_question_set(
    body: InterviewQuestionSetCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = InterviewQuestionSet(
        id=uuid.uuid4(),
        user_id=user.id,
        company_name=body.company_name.strip(),
        job_role=body.job_role.strip(),
        interview_round=body.interview_round.strip(),
        interview_date=body.interview_date,
        candidate_name=body.candidate_name.strip(),
        interviewer=body.interviewer.strip(),
        questions=body.questions.strip(),
        notes=body.notes.strip(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.get("/{set_id}", response_model=InterviewQuestionSetOut)
async def get_question_set(
    set_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(InterviewQuestionSet).where(InterviewQuestionSet.id == set_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Question set not found")
    return _serialize(row)


def _require_author_or_admin(row: InterviewQuestionSet, user: User) -> None:
    if row.user_id != user.id and user.role not in ("admin", "super_admin"):
        # Generic message per F185 — don't name the required role.
        raise HTTPException(
            status_code=403, detail="Insufficient privileges for this action"
        )


@router.patch("/{set_id}", response_model=InterviewQuestionSetOut)
async def update_question_set(
    set_id: UUID,
    body: InterviewQuestionSetUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(InterviewQuestionSet).where(InterviewQuestionSet.id == set_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Question set not found")
    _require_author_or_admin(row, user)

    fields = body.model_fields_set
    for name in (
        "company_name", "job_role", "interview_round", "candidate_name",
        "interviewer", "questions", "notes",
    ):
        if name in fields:
            value = getattr(body, name)
            if value is not None:
                setattr(row, name, value.strip())
    # Date is nullable — an explicit null clears it, omission leaves it.
    if "interview_date" in fields:
        row.interview_date = body.interview_date

    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.delete("/{set_id}", status_code=204)
async def delete_question_set(
    set_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(InterviewQuestionSet).where(InterviewQuestionSet.id == set_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Question set not found")
    _require_author_or_admin(row, user)
    await db.delete(row)
    await db.commit()
