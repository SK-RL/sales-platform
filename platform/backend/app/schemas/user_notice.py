"""Schemas for per-user login notices."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserNoticeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    level: str
    title: str
    body: str
    action_label: str | None = None
    action_href: str | None = None
    created_at: datetime
