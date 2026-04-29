"""Pydantic schemas for feedback / tickets."""

import json
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.sanitize import strip_html_tags


VALID_CATEGORIES = {"bug", "feature_request", "improvement", "question"}
VALID_PRIORITIES = {"low", "medium", "high", "critical"}
VALID_STATUSES = {"open", "in_progress", "resolved", "closed"}


# Regression finding 162: the `title` column was previously stored verbatim,
# so a ticket titled `<img src=x onerror=alert(1)> test probe` survived
# round-trip through the API and any admin renderer that ever switched to
# `dangerouslySetInnerHTML` would execute it. All free-text fields are now
# stripped of HTML at the schema boundary via `strip_html_tags`, which uses
# BeautifulSoup to hard-drop script/style/iframe subtrees AND unwrap every
# remaining tag into plain text. Same defence as the job-description
# sanitizer, but stricter — titles have no legitimate HTML use.
def _strip_html_text(v: str | None) -> str | None:
    if v is None:
        return v
    cleaned = strip_html_tags(v)
    return cleaned

# Upper bound on long free-text fields. Bug reports with call stacks or
# feature requests with product requirements can run long, but prod was
# accepting a 1 MB `description` (see regression finding 25) — this caps
# each field at 8 KB, which is plenty of prose while blocking DB bloat.
_LONG_TEXT_MAX = 8000

# URL schemes allowed on screenshot_url. `javascript:` was accepted prior —
# that field is rendered as a link, so an unrestricted scheme is an XSS
# vector once someone clicks it.
_URL_SAFE_SCHEMES = ("http://", "https://", "/")


def _validate_optional_url(v: str | None) -> str | None:
    if v is None or v == "":
        return v
    stripped = v.strip()
    if len(stripped) > 2048:
        raise ValueError("URL too long (max 2048 chars)")
    low = stripped.lower()
    if not low.startswith(_URL_SAFE_SCHEMES):
        raise ValueError("URL must start with http://, https://, or / (relative)")
    return stripped


class FeedbackCreate(BaseModel):
    # `extra="forbid"` blocks the `extra_evil` / `__proto__` payloads flagged
    # in finding 162 — unknown keys now 422 instead of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    category: str = Field(..., description="bug | feature_request | improvement | question")
    priority: str = Field("medium", description="low | medium | high | critical")
    title: str = Field(..., min_length=5, max_length=200)
    description: str = Field(..., min_length=20, max_length=_LONG_TEXT_MAX)
    steps_to_reproduce: str | None = Field(default=None, max_length=_LONG_TEXT_MAX)
    expected_behavior: str | None = Field(default=None, max_length=_LONG_TEXT_MAX)
    actual_behavior: str | None = Field(default=None, max_length=_LONG_TEXT_MAX)
    use_case: str | None = Field(default=None, max_length=_LONG_TEXT_MAX)
    proposed_solution: str | None = Field(default=None, max_length=_LONG_TEXT_MAX)
    impact: str | None = Field(default=None, max_length=_LONG_TEXT_MAX)
    screenshot_url: str | None = Field(default=None, max_length=2048)

    @field_validator("screenshot_url")
    @classmethod
    def _check_screenshot_url(cls, v):
        return _validate_optional_url(v)

    # F162: strip HTML from every free-text field at the API boundary so
    # `<img onerror=...>` in titles/descriptions becomes harmless plain
    # text regardless of how admin renderers display it. Runs AFTER the
    # length check above, so a payload that's legitimately over the limit
    # still fails validation rather than silently getting trimmed.
    @field_validator(
        "title", "description", "steps_to_reproduce", "expected_behavior",
        "actual_behavior", "use_case", "proposed_solution", "impact",
    )
    @classmethod
    def _strip_html(cls, v):
        return _strip_html_text(v)


class FeedbackUpdate(BaseModel):
    # Same rationale as FeedbackCreate — reject unknown keys instead of
    # quietly accepting defensive-in-depth bypasses like `__proto__`.
    model_config = ConfigDict(extra="forbid")

    # F292 (closes F162 b/c) — Literal-typed status + priority so
    # generic PATCH can't smuggle non-canonical values past the
    # handler's VALID_STATUSES / VALID_PRIORITIES check. Pre-fix
    # ``status: str | None`` accepted any string at the schema
    # layer; the handler's set check (line 411 / 416 / 518) caught
    # it but only AFTER the schema parsed, and only on endpoints
    # that explicitly checked. The Literal makes the contract
    # enforced at parse time across every PATCH path.
    status: Literal["open", "in_progress", "resolved", "closed"] | None = None
    priority: Literal["low", "medium", "high", "critical"] | None = None
    admin_notes: str | None = Field(default=None, max_length=_LONG_TEXT_MAX)

    @field_validator("admin_notes")
    @classmethod
    def _strip_admin_notes(cls, v):
        return _strip_html_text(v)


class FeedbackUserOut(BaseModel):
    id: UUID
    email: str
    name: str
    avatar_url: str

    class Config:
        from_attributes = True


class FeedbackOut(BaseModel):
    id: UUID
    user_id: UUID
    category: str
    priority: str
    status: str
    title: str
    description: str
    steps_to_reproduce: str | None
    expected_behavior: str | None
    actual_behavior: str | None
    use_case: str | None
    proposed_solution: str | None
    impact: str | None
    screenshot_url: str | None
    attachments: list | None = None
    admin_notes: str | None
    resolved_by: UUID | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    user: FeedbackUserOut | None = None
    resolver: FeedbackUserOut | None = None

    class Config:
        from_attributes = True

    @field_validator("attachments", mode="before")
    @classmethod
    def parse_attachments(cls, v):
        # DB stores attachments as a JSON-encoded string; coerce to list before validation.
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
        return []
