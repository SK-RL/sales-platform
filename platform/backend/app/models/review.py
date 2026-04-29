import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, ARRAY, Index, UniqueConstraint
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
        # F281 (closes F156) — exactly one review per (job, reviewer).
        # The handler does a session-level check and then ``db.add``,
        # which races under concurrent submissions: two parallel
        # ``POST /reviews`` for the same (job, reviewer) pair both
        # passed the check and both committed pre-fix. Live probe
        # produced 3 review rows from one user for one job, with two
        # different decisions, each spawning its own
        # ``PotentialClient`` + ``company.is_target=True`` +
        # feedback-task — multiplying the side effects.
        # Migration ``k7l8m9n0o1p2`` dedupes existing rows then adds
        # the UNIQUE INDEX ``uq_reviews_job_reviewer``. Declaring the
        # constraint here too means a fresh
        # ``Base.metadata.create_all()`` (test bootstrap, dev DB)
        # also gets the constraint, matching prod.
        UniqueConstraint(
            "job_id", "reviewer_id",
            name="uq_reviews_job_reviewer",
        ),
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
