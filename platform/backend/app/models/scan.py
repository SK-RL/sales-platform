import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class ScanLog(Base):
    __tablename__ = "scan_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(300), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    # F277 — indexed because /monitoring activity_24h, the F272 prune
    # task, and /platforms last_scan all filter or order by this column.
    # Migration ``i5j6k7l8m9n0`` adds the index as DESC (matching the
    # dominant most-recent-first access pattern); ``index=True`` here
    # so a future ``Base.metadata.create_all()`` (test bootstrap, fresh
    # dev DB) gets a btree(started_at) too instead of silently going
    # back to seq-scan-on-every-monitoring-render.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    updated_jobs: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class CareerPageWatch(Base):
    __tablename__ = "career_page_watches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False)
    last_hash: Mapped[str] = mapped_column(String(64), default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    has_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    check_count: Mapped[int] = mapped_column(Integer, default=0)
    change_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
