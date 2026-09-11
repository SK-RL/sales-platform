import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, JSON, Float, Integer, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    title_normalized: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    location_raw: Mapped[str] = mapped_column(String(500), default="")
    # Raw remote-scope text from the ATS API (e.g. "Remote", "Fully
    # Remote", "Hybrid"). Input to the classifier — distinct from
    # ``remote_policy`` which is the classified bucket.
    remote_scope: Mapped[str] = mapped_column(String(500), default="")
    department: Mapped[str] = mapped_column(String(300), default="")
    employment_type: Mapped[str] = mapped_column(String(100), default="")
    salary_range: Mapped[str] = mapped_column(String(200), default="")

    # Classification (legacy)
    # ``geography_bucket`` is kept for one release after migration
    # d0e1f2g3h4i5 ships so out-of-tree analytics queries keep working.
    # New code reads ``remote_policy`` + ``remote_policy_countries``;
    # the classifier shadow-writes both columns. A follow-up migration
    # will drop this column.
    geography_bucket: Mapped[str] = mapped_column(String(50), default="")  # legacy: global_remote | usa_only | uae_only
    matched_role: Mapped[str] = mapped_column(String(200), default="")
    role_cluster: Mapped[str] = mapped_column(String(50), default="")  # infra | security

    # Classification (new — d0e1f2g3h4i5)
    # ``remote_policy``: enum on the "where can the candidate work
    # from" axis. Values: worldwide | country_restricted |
    # region_restricted | hybrid | onsite | unknown. See
    # ``app/utils/remote_policy.py`` for the canonical definitions.
    # ``remote_policy_countries``: ISO-3166 alpha-2 codes when
    # ``remote_policy="country_restricted"``; empty list otherwise.
    # Sorted, de-duped, upper-cased on write — keeps the GIN index
    # hit rate high.
    remote_policy: Mapped[str] = mapped_column(
        String(32), default="unknown", server_default="unknown", nullable=False
    )
    remote_policy_countries: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    # Scoring
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Workflow status
    status: Mapped[str] = mapped_column(String(50), default="new")  # new | under_review | accepted | rejected | expired | archived

    # Dates
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Raw data
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Provenance (Feature A — manual link submission).
    # `submission_source` = "scan" for every row written by the scan
    # pipeline (the default), "manual_link" for rows created by
    # POST /jobs/submit-link. Kept as a plain string column to match
    # the existing `status` convention. `submitted_by_user_id` is only
    # populated for `manual_link` rows.
    submission_source: Mapped[str] = mapped_column(String(30), default="scan", nullable=False)
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # F374 — aggregator apply-link resolution. Himalayas (and any other
    # aggregator) reposts an employer's posting; the real form lives on
    # the employer's ATS behind an "Apply" redirect. ``apply_url`` is that
    # resolved destination, ``apply_platform`` what the URL fingerprinted
    # as, ``resolved_job_id`` the catalogue Job created from it when the
    # ATS is one we read (so the apply path can use it), and
    # ``apply_resolve_status`` why not otherwise:
    #   resolved | external | blocked | no_link | error
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    apply_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resolved_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    apply_resolve_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    apply_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    company: Mapped["Company"] = relationship()
    description: Mapped["JobDescription | None"] = relationship(back_populates="job", uselist=False, cascade="all, delete-orphan")
    reviews: Mapped[list["Review"]] = relationship(back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_company", "company_id"),
        Index("idx_jobs_platform", "platform"),
        Index("idx_jobs_geography", "geography_bucket"),
        Index("idx_jobs_remote_policy", "remote_policy"),
        # GIN index on remote_policy_countries created in the
        # migration directly — declarative form would need
        # postgresql_using="gin" + a list of expressions, but a plain
        # Index() doesn't render GIN. Migration owns the DDL.
        Index("idx_jobs_score", relevance_score.desc()),
        Index("idx_jobs_first_seen", "first_seen_at"),
    )


class JobDescription(Base):
    __tablename__ = "job_descriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False)
    html_content: Mapped[str] = mapped_column(default="")
    text_content: Mapped[str] = mapped_column(default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    word_count: Mapped[int] = mapped_column(Integer, default=0)

    job: Mapped["Job"] = relationship(back_populates="description")


# Forward references
from app.models.company import Company
from app.models.review import Review
