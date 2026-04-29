"""Pydantic schemas for Role Rule endpoints."""

from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from datetime import datetime
from uuid import UUID


# Regression finding 128: the original schemas had `cluster: str`,
# `base_role: str`, `keywords: list[str]` with zero Field() constraints,
# so every validation fell to the DB layer — resulting in a mix of
# 201 (empty fields passed, wasted rows), 500 (oversize fields hit the
# String(N) limits), and 201-with-HTML (no scheme/charset guards on
# keyword strings). Caps mirror the model columns in `models/rule.py`
# (cluster=50, base_role=200) and sane business limits for the keyword
# array (50 per rule, 60 per keyword). The keyword pattern bans HTML/
# SQL-shaped characters the matcher never uses (`<>'";&`) so a rule
# can't render as script in the admin UI when listed. `list[str]`
# alone would have let `"<script>"` keywords through.
_CLUSTER_MAX_LEN = 50
_BASE_ROLE_MAX_LEN = 200
_KEYWORDS_MAX_COUNT = 50
_KEYWORD_MIN_LEN = 1
_KEYWORD_MAX_LEN = 60
# Matcher uses `ILIKE '%kw%'` so these are the chars a rule can
# actually contain in real configurations: word chars + the punctuation
# that shows up in tech names (`/`, `.`, `-`, `+`, `&`, spaces).
# Everything else is either a typo, a scrape artifact, or an attack.
_KEYWORD_PATTERN = r"^[\w\s\-/.&+]+$"


_Keyword = Annotated[
    str,
    StringConstraints(
        min_length=_KEYWORD_MIN_LEN,
        max_length=_KEYWORD_MAX_LEN,
        strip_whitespace=True,
        pattern=_KEYWORD_PATTERN,
    ),
]


class RoleRuleOut(BaseModel):
    id: UUID
    cluster: str
    base_role: str
    keywords: list[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# Regression finding 178: `list[str]` accepts the empty list, so
# `POST /rules {"cluster":"infra","base_role":"test","keywords":[]}`
# returned 201 with a rule that could never match anything — dead weight
# in the admin UI and, worse, confusing for whoever encountered it later
# wondering why a rule wasn't firing. `null` was already rejected by the
# list_type validator; `[]` now joins it at 422. Also strip out empty
# strings/whitespace-only entries inside the list (same "might as well
# not have a rule" result).
def _require_non_empty_keywords(v: list[str]) -> list[str]:
    if v is None:
        return v
    cleaned = [k.strip() for k in v if isinstance(k, str) and k.strip()]
    if not cleaned:
        raise ValueError("keywords must contain at least one non-empty entry")
    return cleaned


class RoleRuleCreate(BaseModel):
    # F128: match DB column sizes + ban empty strings up front so the
    # "empty base_role" footgun (indistinguishable from "no rule at
    # all" in the matching code) is impossible.
    # F305: ``extra="forbid"`` so admin-side typos like ``custer``
    # 422 instead of silently dropping. Same defense pattern as
    # F130 (review), F131 (company), F162 (feedback), F268 (role
    # cluster), F287 (board).
    model_config = ConfigDict(extra="forbid")

    cluster: str = Field(..., min_length=1, max_length=_CLUSTER_MAX_LEN)
    base_role: str = Field(..., min_length=1, max_length=_BASE_ROLE_MAX_LEN)
    keywords: list[_Keyword] = Field(..., max_length=_KEYWORDS_MAX_COUNT)
    is_active: bool = True

    @field_validator("keywords")
    @classmethod
    def _check_keywords(cls, v):
        return _require_non_empty_keywords(v)


class RoleRuleUpdate(BaseModel):
    # F128: same caps on the PATCH path. Every field is optional so
    # partial updates work, but any value that IS provided must pass
    # the same shape check as POST.
    # F305: ``extra="forbid"`` mirrored from RoleRuleCreate.
    model_config = ConfigDict(extra="forbid")

    cluster: str | None = Field(default=None, min_length=1, max_length=_CLUSTER_MAX_LEN)
    base_role: str | None = Field(default=None, min_length=1, max_length=_BASE_ROLE_MAX_LEN)
    keywords: list[_Keyword] | None = Field(default=None, max_length=_KEYWORDS_MAX_COUNT)
    is_active: bool | None = None

    @field_validator("keywords")
    @classmethod
    def _check_keywords(cls, v):
        # PATCH: allow `keywords` to be omitted entirely, but if it IS
        # provided, hold it to the same non-empty rule as POST.
        if v is None:
            return v
        return _require_non_empty_keywords(v)
