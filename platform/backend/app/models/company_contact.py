"""Company contact and job-contact relevance models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Float, Boolean, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class CompanyContact(Base):
    __tablename__ = "company_contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)

    # Person info
    first_name: Mapped[str] = mapped_column(String(200), default="")
    last_name: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    role_category: Mapped[str] = mapped_column(String(100), default="other")  # executive, engineering_lead, hiring, talent, other
    department: Mapped[str] = mapped_column(String(200), default="")
    seniority: Mapped[str] = mapped_column(String(50), default="other")  # c_suite, vp, director, manager, other

    # Contact channels
    email: Mapped[str] = mapped_column(String(300), default="")
    email_status: Mapped[str] = mapped_column(String(50), default="unverified")  # unverified, valid, invalid, catch_all
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    phone: Mapped[str] = mapped_column(String(50), default="")
    linkedin_url: Mapped[str] = mapped_column(String(500), default="")
    twitter_url: Mapped[str] = mapped_column(String(500), default="")
    telegram_id: Mapped[str] = mapped_column(String(200), default="")

    # Provenance
    source: Mapped[str] = mapped_column(String(100), default="")  # website_scrape, email_pattern, manual, search
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    is_decision_maker: Mapped[bool] = mapped_column(Boolean, default=False)

    # Outreach tracking
    outreach_status: Mapped[str] = mapped_column(String(50), default="not_contacted")  # not_contacted, emailed, replied, meeting_scheduled, not_interested
    outreach_note: Mapped[str] = mapped_column(Text, default="")
    last_outreach_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Timestamps
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    company: Mapped["Company"] = relationship(back_populates="contacts")

    __table_args__ = (
        Index("idx_contacts_company", "company_id"),
        Index("idx_contacts_email", "email"),
        Index("idx_contacts_role_cat", "role_category"),
        # F282 (closes F160) — partial expression unique index. Empty
        # ``email`` is exempt (multiple no-email contacts per company
        # are legitimately separate people). Match must be
        # case-insensitive + whitespace-trimmed to mirror the handler's
        # ``_email_already_exists`` helper. Migration ``l8m9n0o1p2q3``
        # owns the actual DDL — declared here so a fresh
        # ``Base.metadata.create_all()`` (test bootstrap, dev DB)
        # creates the same partial index instead of silently allowing
        # the race.
        Index(
            "uq_company_contacts_company_email",
            "company_id",
            func.lower(func.trim(email)),
            unique=True,
            postgresql_where=(func.trim(email) != ""),
        ),
    )


class JobContactRelevance(Base):
    __tablename__ = "job_contact_relevance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("company_contacts.id", ondelete="CASCADE"), nullable=False)
    relevance_reason: Mapped[str] = mapped_column(String(300), default="")
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_jcr_job", "job_id"),
        Index("idx_jcr_contact", "contact_id"),
    )
