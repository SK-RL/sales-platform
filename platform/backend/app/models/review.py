import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, ARRAY, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(50), nullable=False)  # accepted | rejected | skipped
    comment: Mapped[str] = mapped_column(default="")
    tags: Mapped[list] = mapped_column(ARRAY(String), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    job: Mapped["Job"] = relationship(back_populates="reviews")
    reviewer: Mapped["User"] = relationship()

    __table_args__ = (
        # F278 — composite index for the dominant read pattern across
        # ai_insights (per-user, last-30-days), /reviews/queue listing,
        # and audit lookups. ``reviewer_id`` first (equality, most
        # selective), ``created_at DESC`` second (range filter +
        # most-recent-first ORDER BY). Migration ``j6k7l8m9n0o1`` owns
        # the actual DDL; this declaration mirrors it so a fresh
        # ``Base.metadata.create_all()`` (test bootstrap, dev DB) gets
        # the same index instead of silently regressing to seq scan.
        Index(
            "idx_reviews_reviewer_created",
            "reviewer_id",
            created_at.desc(),
        ),
    )


from app.models.job import Job
from app.models.user import User
