"""Interview question repository — ticket 8ef0e9c2.

One row per interview debrief: after a candidate finishes an
interview round with a client company, the questions they were asked
get recorded here so future candidates interviewing with the same
client can prepare from real data.

Deliberately denormalised: ``company_name`` is a free-text string
(with an optional soft link to a ``Company`` row when one matches)
because debriefs frequently reference companies that aren't in the
scraped corpus, and blocking a debrief on company-record creation
would kill adoption. The repository's value is recall, not
referential purity.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class InterviewQuestionSet(Base):
    __tablename__ = "interview_question_sets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # SET NULL on user delete — debriefs are institutional memory and
    # must outlive the account that recorded them.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    job_role: Mapped[str] = mapped_column(String(200), nullable=False)
    # Free text ("HR", "Technical Round 1", "System Design", ...) —
    # rounds are client-specific; an enum would fight reality.
    interview_round: Mapped[str] = mapped_column(String(100), nullable=False)
    interview_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    candidate_name: Mapped[str] = mapped_column(
        String(200), default="", server_default=""
    )
    interviewer: Mapped[str] = mapped_column(
        String(300), default="", server_default=""
    )
    # The payload — one question per line by convention; the frontend
    # renders/edits as a textarea and displays as a numbered list.
    questions: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    author = relationship("User", foreign_keys=[user_id], lazy="joined")
