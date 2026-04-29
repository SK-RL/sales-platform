"""Pydantic schemas for company contacts and offices.

Regression finding 133 (stored XSS + validation holes):
`POST /companies/{id}/contacts` previously accepted `javascript:`
URLs on `linkedin_url` / `twitter_url` (same class as F77 on
`PlatformCredential.profile_url` — fixed there but overlooked on
the parallel contact endpoint), empty-body create (ghost rows with
all fields blank), unbounded `outreach_note` (1 MB persisted blob
DoSs the table), `title` > 300 chars (DB VARCHAR(300) cap without
Pydantic cap → HTTP 500 F128/F132 pattern), and plaintext `email`
(no shape validation at all, garbage feeds downstream workers).

Five coordinated fixes in this module:
  (1) URL scheme allowlist on every user-writable URL field via a
      shared `_validate_optional_url` helper (mirrors F77's pattern
      in schemas/credential.py).
  (2) `@model_validator(mode="after")` on CompanyContactCreate
      requiring at least ONE of (first_name, last_name, email) to
      be non-empty — stops POST {} ghost rows without breaking
      partial-data imports that have e.g. just an email + linkedin.
  (3) `max_length` on every String() column to mirror the DB width,
      so oversize payloads 422 at parse time instead of 500'ing on
      the INSERT (title 300, phone 50, URLs 500, telegram 200, etc.).
  (4) `outreach_note` bounded at 2000 chars on both CompanyContactUpdate
      and OutreachUpdate. DB column is Text (unbounded) so this is
      an app-level DoS defense rather than a schema mirror.
  (5) Soft email validation — keep `str` default `""` so callers
      without an email address can still record a contact (common
      for recruiter-only contacts), but require `@` + `.` when
      non-empty. Full EmailStr would 422 on an empty default; this
      validator only fires if the caller passed a non-empty value.

The frontend renders `linkedin_url` / `twitter_url` in
`<a href={url}>` tags on CompanyDetailPage, JobDetailPage, and
IntelligencePage (three distinct XSS surfaces), so server-side
validation is the right place to cut this off — a defense-in-depth
client-side `sanitizeUrl()` helper is a separate round of work.
"""

from typing import Literal
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.sanitize import strip_html_tags


# F289 (closes F148 a+c) additional hardening on the contact
# schemas:
#   (a) ``first_name`` / ``last_name`` / ``title`` flow into the
#       admin UI; pre-F289 they accepted unsanitized HTML so a
#       payload like ``first_name="<script>alert(1)</script>"``
#       rendered verbatim in the SPA. Defense: ``strip_html_tags``
#       on both create + update paths via the shared
#       ``_strip_html_tag`` helper.
#   (c) ``outreach_status`` had a length cap but NO Literal
#       constraint. The dedicated /outreach endpoint enforces
#       ``_VALID_OUTREACH`` in the handler, but the generic PATCH
#       /contacts/{id} bypassed that. The Literal below is the
#       single source of truth, mirrored in the handler's
#       ``_VALID_OUTREACH`` constant.
_OUTREACH_STATUSES = (
    "not_contacted", "emailed", "replied", "meeting_scheduled", "not_interested",
)
_OutreachStatus = Literal[
    "not_contacted", "emailed", "replied", "meeting_scheduled", "not_interested",
]


def _strip_html(v):
    """Pydantic field-validator helper: strip HTML tags from
    text fields that flow into the admin UI. ``None`` and empty
    pass through unchanged.
    """
    if v is None or v == "":
        return v
    return strip_html_tags(v).strip()


# URL schemes permitted on any user-writable URL field rendered by
# the SPA as `<a href>` / `<img src>`. Must stay in sync with the
# same list in schemas/credential.py::_URL_SAFE_SCHEMES. `javascript:`,
# `data:`, `vbscript:`, `file:`, and bare fragments are all rejected
# — React JSX does NOT sanitize `href` values and `rel=noopener` does
# not block scheme execution.
_URL_SAFE_SCHEMES = ("http://", "https://", "/")


def _validate_optional_url(v: str | None) -> str | None:
    """Shared URL-scheme allowlist. Returns the input on success,
    raises ValueError (→ 422) on disallowed schemes. Empty / None
    pass through unchanged — contacts commonly lack a social URL."""
    if v is None or v == "":
        return v
    stripped = v.strip()
    if not stripped:
        return ""
    low = stripped.lower()
    if not low.startswith(_URL_SAFE_SCHEMES):
        raise ValueError(
            "URL must start with http://, https://, or / (relative)"
        )
    return stripped


def _validate_optional_email(v: str | None) -> str | None:
    """Soft email validator — empty / None pass through. Non-empty
    values must contain `@` and `.` to catch the most obvious garbage
    without the Pydantic EmailStr DNS-shape strictness that breaks on
    quoted local parts, IDN domains, etc. (downstream SMTP validator
    does the final check before actually sending mail)."""
    if v is None or v == "":
        return v
    stripped = v.strip()
    if not stripped:
        return ""
    # F133(c): "not-an-email" / "foo" / "user@" should 422 at parse.
    if "@" not in stripped or "." not in stripped.split("@", 1)[-1]:
        raise ValueError(
            "email must contain '@' and a domain (or leave blank)"
        )
    return stripped


class CompanyContactOut(BaseModel):
    id: UUID
    company_id: UUID
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    role_category: str = "other"
    department: str = ""
    seniority: str = "other"
    email: str = ""
    email_status: str = "unverified"
    email_verified_at: datetime | None = None
    phone: str = ""
    linkedin_url: str = ""
    twitter_url: str = ""
    telegram_id: str = ""
    source: str = ""
    confidence_score: float = 0.0
    is_decision_maker: bool = False
    outreach_status: str = "not_contacted"
    outreach_note: str = ""
    last_outreach_at: datetime | None = None
    last_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CompanyContactCreate(BaseModel):
    # F133(3): max_length mirrors DB column widths in models/company_contact.py.
    first_name: str = Field(default="", max_length=200)
    last_name: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=300)
    role_category: str = Field(default="other", max_length=100)
    department: str = Field(default="", max_length=200)
    seniority: str = Field(default="other", max_length=50)
    email: str = Field(default="", max_length=300)
    phone: str = Field(default="", max_length=50)
    linkedin_url: str = Field(default="", max_length=500)
    twitter_url: str = Field(default="", max_length=500)
    telegram_id: str = Field(default="", max_length=200)
    is_decision_maker: bool = False

    # F133(1): URL scheme allowlist — blocks `javascript:alert(1)` XSS
    # vector at the API boundary before it reaches DB / SPA render.
    @field_validator("linkedin_url", "twitter_url")
    @classmethod
    def _check_urls(cls, v):
        return _validate_optional_url(v)

    # F133(5): shape-validate non-empty emails.
    @field_validator("email")
    @classmethod
    def _check_email(cls, v):
        return _validate_optional_email(v)

    # F289 (closes F148 a): strip HTML from name/title/department
    # fields that render in the admin UI. Same defense pattern
    # applied across feedback (F162), role-cluster display_name
    # (F285), platforms board company_name (F287).
    @field_validator("first_name", "last_name", "title", "department")
    @classmethod
    def _strip_html_fields(cls, v):
        return _strip_html(v)

    # F133(2): reject POST {} (ghost-row DoS). At least one identifying
    # field must be present — first_name, last_name, or email. Callers
    # importing a partially-scraped contact (e.g. "found a linkedin URL
    # but no name") must still provide one of those three to anchor the
    # row.
    @model_validator(mode="after")
    def _require_identifier(self):
        if not (self.first_name.strip() or self.last_name.strip() or self.email.strip()):
            raise ValueError(
                "At least one of first_name, last_name, or email is required"
            )
        return self


class CompanyContactUpdate(BaseModel):
    # F133(3): same max_length mirrors as Create — PATCH can 500 the
    # same way if an oversize field slips through.
    first_name: str | None = Field(default=None, max_length=200)
    last_name: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=300)
    role_category: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=200)
    seniority: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=50)
    linkedin_url: str | None = Field(default=None, max_length=500)
    twitter_url: str | None = Field(default=None, max_length=500)
    telegram_id: str | None = Field(default=None, max_length=200)
    is_decision_maker: bool | None = None
    # F289 (closes F148 c): Literal so generic PATCH can't smuggle
    # a non-canonical outreach_status past the dedicated /outreach
    # endpoint's ``_VALID_OUTREACH`` check. Source of truth lives
    # at module scope (``_OutreachStatus``) — keep the handler
    # constant in sync.
    outreach_status: _OutreachStatus | None = None
    # F133(4): outreach_note DB column is Text (unbounded). Cap at 2000
    # chars at the app layer — longer than any sane recruiter note,
    # short enough that a flood of them can't DoS the table.
    outreach_note: str | None = Field(default=None, max_length=2000)

    @field_validator("linkedin_url", "twitter_url")
    @classmethod
    def _check_urls(cls, v):
        return _validate_optional_url(v)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v):
        return _validate_optional_email(v)

    # F289 (closes F148 a): mirror the create-side HTML strip on
    # the PATCH path. Pre-fix you could create a clean contact and
    # PATCH the name fields to a payload, bypassing the create-side
    # guard.
    @field_validator("first_name", "last_name", "title", "department")
    @classmethod
    def _strip_html_fields_update(cls, v):
        return _strip_html(v)


class OutreachUpdate(BaseModel):
    # F289: also Literal-typed so the dedicated /outreach endpoint
    # gets schema-level validation in addition to the handler's
    # ``_VALID_OUTREACH`` check (defense in depth — the handler
    # check stays as a comment-friendly second layer).
    outreach_status: _OutreachStatus = Field(...)
    # F133(4): same 2000-char cap as CompanyContactUpdate.outreach_note.
    outreach_note: str = Field(default="", max_length=2000)


class CompanyOfficeOut(BaseModel):
    id: UUID
    company_id: UUID
    label: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    is_headquarters: bool = False
    source: str = ""

    model_config = {"from_attributes": True}


class JobRelevantContact(BaseModel):
    contact: CompanyContactOut
    relevance_reason: str = ""
    relevance_score: float = 0.0
