from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime
from uuid import UUID


# Reasonable bounds for priority (UI uses 0..3 today; 100 leaves headroom without
# letting anyone push 10-digit values that break sort comparators) and notes
# (plenty for deal context, cheap to store, blocks a 100 KB+ payload DOS that
# was landing in prod — regression finding 15).
PIPELINE_MAX_PRIORITY = 100
PIPELINE_MAX_NOTES_LENGTH = 4000


class PipelineItemOut(BaseModel):
    id: UUID
    company_id: UUID | None = None
    company_name: str | None = None
    company_website: str | None = None
    stage: str
    priority: int
    assigned_to: UUID | None
    resume_id: UUID | None = None
    applied_by: UUID | None = None
    applied_by_name: str | None = None
    resume_label: str | None = None
    enrichment_data: dict
    enriched_at: datetime | None
    accepted_jobs_count: int
    total_open_roles: int
    hiring_velocity: str
    # F266 — count of submitted applications under this card's company.
    # Pre-fix the kanban gave no signal that 19/23 cards had zero
    # applications behind them (admin clicked "Apps" → empty panel,
    # repeated). Surfaced on the card so admins can see at a glance
    # which targets actually have apply activity vs which are still
    # research-only / stalled-after-accept. Defaults to 0 for legacy
    # rows; populated by /pipeline + /pipeline/{id} via a single
    # aggregated query.
    applications_count: int = 0
    notes: str
    # Ticket bac45b42 — free-form fields for manually-created cards.
    # Empty dict for scan-sourced cards; see ManualCardFields for the
    # documented shape.
    manual_card: dict = {}
    created_at: datetime

    model_config = {"from_attributes": True}


class PipelineUpdate(BaseModel):
    # F268 — extra="forbid" so admin typos in the kanban edit modal
    # 422 instead of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    stage: str | None = None
    priority: int | None = Field(default=None, ge=0, le=PIPELINE_MAX_PRIORITY)
    assigned_to: UUID | None = None
    resume_id: UUID | None = None
    applied_by: UUID | None = None
    notes: str | None = Field(default=None, max_length=PIPELINE_MAX_NOTES_LENGTH)


class ManualCardFields(BaseModel):
    """Documented shape of ``PotentialClient.manual_card`` (JSONB).

    Ticket bac45b42. Mandatory-vs-optional is enforced on
    :class:`ManualPipelineCardRequest` (the create payload) — this
    model is the storage contract: everything optional, unknown keys
    forbidden so typos in future writers 422 instead of silently
    persisting.
    """

    model_config = ConfigDict(extra="forbid")

    jd_link: str | None = Field(default=None, max_length=1000)
    applied_id: str | None = Field(
        default=None, max_length=300,
        description="Email or name of the identity used to apply.",
    )
    linkedin_url: str | None = Field(default=None, max_length=500)
    funding_status: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=200)
    salary_current: str | None = Field(default=None, max_length=100)
    salary_expected: str | None = Field(default=None, max_length=100)
    interviewer_name: str | None = Field(default=None, max_length=200)
    interviewer_email: str | None = Field(default=None, max_length=300)
    interviewee_name: str | None = Field(default=None, max_length=200)
    interviewee_type: str | None = Field(
        default=None, max_length=100,
        description="Description type of the interviewee (per ticket).",
    )
    jd_description: str | None = Field(default=None, max_length=8000)
    details: str | None = Field(default=None, max_length=8000)


class ManualPipelineCardRequest(BaseModel):
    """Create-payload for ``POST /pipeline/manual`` (ticket bac45b42).

    The four mandatory ("restricted") fields per the ticket:
    company name, company website, JD link, applied identity. The
    company is found-or-created by slugified name — manual cards must
    not be blocked on the company already existing in the scraped
    corpus.
    """

    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1, max_length=300)
    company_website: str = Field(min_length=4, max_length=500)
    jd_link: str = Field(min_length=4, max_length=1000)
    applied_id: str = Field(min_length=1, max_length=300)

    stage: str | None = Field(
        default=None,
        description="Pipeline stage key; defaults to the first active stage.",
    )
    priority: int = Field(default=0, ge=0, le=PIPELINE_MAX_PRIORITY)
    notes: str = Field(default="", max_length=PIPELINE_MAX_NOTES_LENGTH)

    linkedin_url: str | None = Field(default=None, max_length=500)
    funding_status: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=200)
    salary_current: str | None = Field(default=None, max_length=100)
    salary_expected: str | None = Field(default=None, max_length=100)
    interviewer_name: str | None = Field(default=None, max_length=200)
    interviewer_email: str | None = Field(default=None, max_length=300)
    interviewee_name: str | None = Field(default=None, max_length=200)
    interviewee_type: str | None = Field(default=None, max_length=100)
    jd_description: str | None = Field(default=None, max_length=8000)
    details: str | None = Field(default=None, max_length=8000)
