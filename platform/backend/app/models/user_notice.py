"""Per-user login notices (in-app banners).

A lightweight admin→user notification primitive: a row targets one user
with a short message that the frontend shows as a dismissible banner on
their next login / page load. Built for the "re-upload your lost
documents" comms after the storage-loss incident (feedback 650514ad),
but intentionally generic so any future "tell these users X on login"
need reuses it instead of another bespoke channel.

Distinct from ``AlertConfig`` (outbound job-alert webhooks) — this is an
inbound banner the user sees inside the app.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class UserNotice(Base):
    __tablename__ = "user_notices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # ON DELETE CASCADE — a deleted user's notices go with them.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # info | warning | critical — drives the banner colour on the client.
    level: Mapped[str] = mapped_column(String(20), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Optional call-to-action. ``action_href`` is an in-app SPA path
    # (e.g. "/profiles"), never an external URL — the frontend renders it
    # as a react-router link.
    action_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action_href: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # Null = still showing. Set when the user clicks "dismiss"; the
    # /notices/me feed only returns rows where this is null.
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
