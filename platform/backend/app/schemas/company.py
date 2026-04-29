"""Pydantic schemas for Company and ATS Board endpoints."""

import json
from typing import Annotated, Any
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field, field_validator
from datetime import datetime
from uuid import UUID


# Regression finding 131: admin PATCH on /companies/{id} used to let
# any admin persist a 1 MB description, 10k tags, or a 500-level nested
# metadata_json dict (which 500'd instead of 422'd). The `companies`
# table is on the critical read path (`/companies` list pulls
# `description` on every page render), so a single bloated row degrades
# every search. Caps mirror the model columns where possible and
# business-reasonable limits where the DB column is `Text` / `JSON`
# (unbounded). Same failure-mode class as F128 (rules), F129
# (applications), F130 (reviews) — standardise the fix shape across
# all admin writer endpoints.
_DESCRIPTION_MAX_LEN = 5000  # typical marketing-copy length; 1MB probe was pure DoS
_TAGS_MAX_COUNT = 50
_TAG_MAX_LEN = 40
_METADATA_JSON_MAX_BYTES = 10_000  # serialized JSON size ceiling
_METADATA_JSON_MAX_DEPTH = 10      # reject deeply-nested recursion bombs


# Per-tag StringConstraints — `\w` + hyphen pattern matches the
# existing tag vocabulary in the wild; strip whitespace so a sloppy
# copy-paste doesn't become a distinct "  tag  " row.
_CompanyTag = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=_TAG_MAX_LEN,
        strip_whitespace=True,
        pattern=r"^[\w\-]+$",
    ),
]


def _metadata_depth(obj: Any, current: int = 0) -> int:
    """Max nesting depth of a JSON-serializable value.

    Used to reject recursion bombs before they reach psycopg2's JSON
    parser, which bails at ~500 levels with a 500 instead of a clean
    422. Walks dict values and list items; scalar leaves contribute
    `current`.
    """
    if isinstance(obj, dict):
        if not obj:
            return current
        return max(_metadata_depth(v, current + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return current
        return max(_metadata_depth(v, current + 1) for v in obj)
    return current


def _validate_metadata_json(v: dict | None) -> dict | None:
    """Shared validator for CompanyCreate + CompanyUpdate `metadata_json`.

    Rejects (a) serialized bodies larger than _METADATA_JSON_MAX_BYTES,
    (b) nesting deeper than _METADATA_JSON_MAX_DEPTH. Runs on the
    already-parsed Python dict — cheaper than parsing the raw body
    twice, and Pydantic has already enforced the type as `dict`.
    """
    if v is None:
        return v
    # Serialize once to measure size (len(str(dict)) is wrong — it
    # reports the Python repr, not JSON; counts differ meaningfully
    # for nested/unicode inputs).
    try:
        serialized = json.dumps(v)
    except (TypeError, ValueError) as err:
        raise ValueError(f"metadata_json must be JSON-serializable: {err}")
    if len(serialized) > _METADATA_JSON_MAX_BYTES:
        raise ValueError(
            f"metadata_json too large ({len(serialized)} bytes > "
            f"{_METADATA_JSON_MAX_BYTES} limit)"
        )
    if _metadata_depth(v) > _METADATA_JSON_MAX_DEPTH:
        raise ValueError(
            f"metadata_json nested too deeply (max depth "
            f"{_METADATA_JSON_MAX_DEPTH})"
        )
    return v

# ATS board URL patterns
ATS_URL_PATTERNS = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "workable": "https://apply.workable.com/{slug}",
    "bamboohr": "https://{slug}.bamboohr.com/careers",
    "himalayas": "https://himalayas.app/companies/{slug}/jobs",
    "wellfound": "https://wellfound.com/company/{slug}/jobs",
    "jobvite": "https://jobs.jobvite.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
    "recruitee": "https://{slug}.recruitee.com",
    "linkedin": "https://www.linkedin.com/company/{slug}/jobs",
}


class ATSBoardOut(BaseModel):
    id: UUID
    company_id: UUID
    platform: str
    slug: str
    is_active: bool
    last_scanned_at: datetime | None = None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def board_url(self) -> str:
        pattern = ATS_URL_PATTERNS.get(self.platform, "")
        return pattern.format(slug=self.slug) if pattern else ""


class ATSBoardCreate(BaseModel):
    """``POST /companies/{id}/ats-boards`` body.

    F303: pre-fix this was bare ``platform: str`` + ``slug: str``
    with no length cap, no ``extra="forbid"``, no slug pattern.
    F128 pattern recurrence — typos like ``platfrm`` silently
    dropped, oversized values crashed the underlying ``String(N)``
    column writer with HTTP 500. The handler in ``companies.py``
    does runtime platform validation against ``FETCHER_MAP``, but
    that fires AFTER schema parsing — adding the schema-layer
    constraints here means malformed input 422s at parse time
    with a useful field-level error instead of 500'ing later.
    """
    model_config = ConfigDict(extra="forbid")

    # Platform is one of ~10 hardcoded fetchers. The handler
    # validates against ``FETCHER_MAP`` so we don't pin a Literal
    # here (a fetcher addition would require updating both); the
    # length cap matches the runtime check's expectation.
    platform: str = Field(..., min_length=1, max_length=50)
    # Slug is the company-handle on the ATS provider's URL. ASCII-
    # safe, kebab/snake-case in practice. The 200-char cap matches
    # the ``CompanyATSBoard.slug`` column width; the pattern is
    # generous enough for real-world slugs (Workable allows mixed-
    # case alphanumerics with dashes/underscores).
    slug: str = Field(..., min_length=1, max_length=200)
    is_active: bool = True


class CompanyOut(BaseModel):
    id: UUID
    name: str
    slug: str
    website: str | None = ""
    logo_url: str = ""
    industry: str = ""
    employee_count: str = ""
    funding_stage: str = ""
    headquarters: str = ""
    description: str = ""
    is_target: bool = False
    tags: list[str] = []
    metadata_json: dict = {}
    domain: str = ""
    founded_year: int | None = None
    total_funding: str = ""
    linkedin_url: str = ""
    twitter_url: str = ""
    tech_stack: list[str] = []
    enrichment_status: str = "pending"
    enriched_at: datetime | None = None
    funded_at: datetime | None = None
    funding_news_url: str = ""
    # Phase A — ATS-lockdown fallback foundation. The URL on the
    # company's OWN site that lists their jobs (distinct from
    # `website`, which is the homepage). Populated by the ATS
    # fingerprint Celery task. NULL until fingerprinted — admins
    # can use this field to see which companies we'd have a
    # fallback path to, if the aggregator ATS ever locks down.
    careers_url: str | None = None
    careers_url_fetched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    ats_boards: list[ATSBoardOut] = []
    job_count: int = 0
    # Regression finding 98: `relevant_job_count` was referenced on the
    # frontend (CompaniesPage renders `{company.relevant_job_count ?? "?"}`)
    # but was never populated or declared in the schema — so the list
    # endpoint returned undefined for every row and the UI showed "?"
    # across the table. Populated in `companies.py::list_companies` via
    # a correlated subquery over `Job.role_cluster IN relevant_clusters`.
    relevant_job_count: int = 0
    accepted_count: int = 0
    contact_count: int = 0

    model_config = {"from_attributes": True}


from app.schemas.company_contact import CompanyContactOut, CompanyOfficeOut


class CompanyDetailOut(CompanyOut):
    """Extended company schema with contacts and offices."""
    contacts: list[CompanyContactOut] = []
    offices: list[CompanyOfficeOut] = []
    actively_hiring: bool = False
    hiring_velocity: str = ""
    total_open_roles: int = 0
    enrichment_error: str = ""


class CompanyCreate(BaseModel):
    # F131: caps matched to model column sizes (see models/company.py).
    # `extra="forbid"` rejects stale-schema fields loudly — same
    # rationale as F130's review schema. All optional-default fields
    # keep their existing empty-string/empty-list defaults.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=300)
    slug: str = Field(..., min_length=1, max_length=300)
    website: str = Field(default="", max_length=500)
    logo_url: str = Field(default="", max_length=500)
    industry: str = Field(default="", max_length=200)
    employee_count: str = Field(default="", max_length=50)
    funding_stage: str = Field(default="", max_length=100)
    headquarters: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=_DESCRIPTION_MAX_LEN)
    is_target: bool = False
    tags: list[_CompanyTag] = Field(default_factory=list, max_length=_TAGS_MAX_COUNT)
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("metadata_json")
    @classmethod
    def _check_metadata(cls, v):
        return _validate_metadata_json(v)


class CompanyUpdate(BaseModel):
    # F131: every field stays optional (PATCH semantics), but any
    # value provided now passes the same caps as CompanyCreate.
    # `extra="forbid"` catches typoed field names client-side instead
    # of silently ignoring them.
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=300)
    slug: str | None = Field(default=None, min_length=1, max_length=300)
    website: str | None = Field(default=None, max_length=500)
    logo_url: str | None = Field(default=None, max_length=500)
    industry: str | None = Field(default=None, max_length=200)
    employee_count: str | None = Field(default=None, max_length=50)
    funding_stage: str | None = Field(default=None, max_length=100)
    headquarters: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=_DESCRIPTION_MAX_LEN)
    is_target: bool | None = None
    tags: list[_CompanyTag] | None = Field(default=None, max_length=_TAGS_MAX_COUNT)
    metadata_json: dict | None = None

    @field_validator("metadata_json")
    @classmethod
    def _check_metadata(cls, v):
        return _validate_metadata_json(v)
