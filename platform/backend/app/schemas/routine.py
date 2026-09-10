"""Pydantic schemas for the Claude Routine Apply feature.

Everything the routine / UI exchange over the wire is declared here.
Kept in one file because these types only make sense as a set — a
submission references a run, a run contains submissions, and the
confirm-submitted payload is what mints both.

Validation choices:
- All fields capped with explicit max lengths (Finding 80 / 183 class:
  unbounded Text columns inflate responses and invite DOS).
- ``model_config = ConfigDict(extra="forbid")`` on every request
  model so a frontend typo 422s instead of silently dropping fields.
- Enum values expressed as ``Literal[...]`` at the boundary, not
  free-text — consistent with existing cover-letter / credentials
  schemas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ═══════════════════════════════════════════════════════════════════
# Allowed enums
# ═══════════════════════════════════════════════════════════════════

RoutineMode = Literal["dry_run", "live", "single_trial"]
RoutineStatus = Literal["running", "complete", "aborted"]
AnswerSource = Literal["manual_required", "learned", "generated"]


# ═══════════════════════════════════════════════════════════════════
# Answer Book — required-setup coverage
# ═══════════════════════════════════════════════════════════════════

class RequiredCoverageEntry(BaseModel):
    id: UUID
    category: str
    question: str
    question_key: str
    answer: str  # empty string if not yet filled
    filled: bool


class RequiredCoverageResponse(BaseModel):
    """Returned by GET /answer-book/required-coverage.

    Pre-flight gate for the routine: if ``complete=False`` the routine
    refuses to run any application (the UI surfaces the ``missing``
    list on /answer-book/required-setup).

    ``entries`` (added in phase-2 improvements) carries *every* required
    row — filled and unfilled — in canonical seed order, so the UI can
    render a stable list that lets the operator EDIT a previously
    filled answer (salary floor changed, notice period changed, etc.).
    ``missing`` is retained for backward compatibility: older frontends
    that only render unfilled rows keep working without a schema round-
    trip.
    """

    complete: bool
    total_required: int
    total_filled: int
    missing: list[RequiredCoverageEntry]  # entries with answer == ""
    entries: list[RequiredCoverageEntry]  # ALL required rows (filled + unfilled)


class SeedRequiredResponse(BaseModel):
    """Returned by POST /answer-book/seed-required."""

    created: int  # newly-inserted this call
    already_present: int  # skipped (idempotency)
    total: int  # total required entries (should equal 16)


# ═══════════════════════════════════════════════════════════════════
# Applications — confirm-submitted + detail
# ═══════════════════════════════════════════════════════════════════

class SubmittedAnswer(BaseModel):
    """One Q&A pair as sent to the ATS.

    ``source_ref_id`` is required for ``manual_required`` / ``learned``
    (points to answer_book_entries.id) and null for ``generated``.
    The handler validates the cross-reference.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=2000)
    answer: str = Field(..., max_length=20000)  # JD answers can get long
    source: AnswerSource
    source_ref_id: UUID | None = None
    # Levenshtein(draft, final); 0 for non-generated answers.
    edit_distance: int = Field(default=0, ge=0)
    # For generated answers: original LLM output before humanization.
    # Captured into humanization_corpus when non-null.
    draft_text: str | None = Field(default=None, max_length=20000)


class ConfirmSubmittedRequest(BaseModel):
    """Body of POST /applications/{id}/confirm-submitted.

    Sent by the routine after a successful browser submit. The handler
    writes the application_submission row, flips Application.status,
    creates the accepted Review row (matches /reviews/apply pattern),
    dispatches scoring-signal feedback, and writes an audit log —
    all in one transaction.
    """

    model_config = ConfigDict(extra="forbid")

    submitted_at: datetime
    job_url: str = Field(..., min_length=1, max_length=2000)
    ats_platform: str = Field(..., min_length=1, max_length=50)
    form_fingerprint_hash: str | None = Field(default=None, max_length=64)
    # PII firewall: payload_json MUST NOT contain keys matching
    # /ssn|social_security|date_of_birth|dob/i — the handler rejects
    # with 400 if it does. Validated there, not here, because the
    # check needs field-name inspection that's awkward in a validator.
    payload_json: dict[str, Any]
    answers: list[SubmittedAnswer]
    resume_version_hash: str | None = Field(default=None, max_length=64)
    cover_letter_text: str | None = Field(default=None, max_length=50000)
    screenshot_keys: list[str] = Field(default_factory=list, max_length=20)
    confirmation_text: str | None = Field(default=None, max_length=5000)
    detected_issues: list[str] = Field(default_factory=list, max_length=50)
    # Frozen manual_required values; {question_key: answer}.
    profile_snapshot: dict[str, str] = Field(default_factory=dict)
    # Null when invoked outside a routine run (should not happen in
    # practice for v6; placeholder for future manual-override paths).
    routine_run_id: UUID | None = None
    # When True, skip the live-apply side-effects (status flip,
    # accepted Review, scoring feedback, pipeline advance). Still
    # writes the application_submission row + audit entry so the
    # user can review what WOULD have been sent. Set by the routine
    # when mode in ('dry_run', 'single_trial').
    dry_run: bool = False


class ConfirmSubmittedResponse(BaseModel):
    application_id: UUID
    submission_id: UUID
    pipeline_entry_id: UUID | None  # None when no PotentialClient was created/updated
    dry_run: bool
    detected_issues: list[str]  # final list (may include platform-sync failures)


class PromoteAnswerRequest(BaseModel):
    """Promote a generated answer from a submission into the answer book."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=2000)
    answer: str = Field(..., min_length=1, max_length=8000)


class PromoteAnswerResponse(BaseModel):
    answer_book_entry_id: UUID
    already_existed: bool  # True if the question_key was already in the book


class SubmissionDetail(BaseModel):
    """Returned by GET /applications/{id}/submission."""

    id: UUID
    application_id: UUID
    routine_run_id: UUID | None
    submitted_at: datetime
    job_url: str
    ats_platform: str
    form_fingerprint_hash: str | None
    payload_json: dict[str, Any]
    answers_json: list[dict[str, Any]]
    resume_version_hash: str | None
    cover_letter_text: str | None
    screenshot_keys: list[str]
    confirmation_text: str | None
    detected_issues: list[str]
    profile_snapshot: dict[str, str]
    created_at: datetime


# ═══════════════════════════════════════════════════════════════════
# Routine runs + top-to-apply + kill switch
# ═══════════════════════════════════════════════════════════════════

class TopToApplyJob(BaseModel):
    job_id: UUID
    title: str
    company_id: UUID | None
    company_name: str
    platform: str
    relevance_score: float
    geography_bucket: str | None
    role_cluster: str | None
    # F257: tells the UI which rows are operator-pinned via the manual
    # queue (``queued`` intent) vs. auto-picked by the relevance
    # picker. Pinned rows render with a small "queued" badge so the
    # operator can confirm their override took effect.
    is_queued: bool = False


class TopToApplyResponse(BaseModel):
    kill_switch_active: bool
    daily_cap_remaining: int
    answer_book_ready: bool  # cached copy of required-coverage.complete
    jobs: list[TopToApplyJob]


# ═══════════════════════════════════════════════════════════════════
# F257 — Routine preferences + manual queue
# ═══════════════════════════════════════════════════════════════════


# Allow-list for ``RoutineTarget.intent`` enforced at the API boundary.
# Mirrors ``ROUTINE_TARGET_INTENTS`` in the model — keep them in sync.
RoutineTargetIntent = Literal["queued", "excluded"]

# Geography buckets the routine knows how to handle (must match the
# existing ``ROUTINE_GEOGRAPHY_BUCKETS`` in the routine router). Kept
# as a Literal so a typo on the frontend 422s at parse time instead of
# silently emptying the picker.
GeographyBucket = Literal["global_remote", "usa_only", "uae_only"]


class RoutinePreferences(BaseModel):
    """Per-user filter preferences applied by ``GET /routine/top-to-apply``.

    Every field is optional with a sensible default — a fresh user
    with an empty ``users.routine_preferences`` JSONB column gets the
    legacy behaviour (no extra filtering).

    Wire shape mirrors ``users.routine_preferences`` in the DB. New
    fields can be added here AND read defensively in the picker
    without a migration.
    """
    model_config = ConfigDict(extra="forbid")

    # Convenience toggle: when True, picker keeps only
    # ``geography_bucket="global_remote"`` regardless of
    # ``allowed_geographies``. Default off so existing users see no
    # behaviour change.
    only_global_remote: bool = False
    # Subset of geography buckets the routine should consider. Empty
    # list means "all". A list overrides
    # ``only_global_remote=False`` only insofar as the picker still
    # filters to this subset.
    allowed_geographies: list[GeographyBucket] = Field(default_factory=list)
    # Floor on ``Job.relevance_score`` (0-100). 0 = no floor.
    min_relevance_score: int = Field(default=0, ge=0, le=100)
    # Floor on the user's resume-fit score for this job (0-100).
    # 0 = no floor. The picker joins to the user's active resume's
    # ResumeScore row; jobs without a score get treated as score=0
    # so a non-zero floor effectively requires the resume to have
    # been scored against them already.
    min_resume_score: int = Field(default=0, ge=0, le=100)
    # Allow-list of role clusters. Empty = use the platform-wide
    # "relevant clusters" (infra/security default). Lets a security-
    # only user exclude infra and vice versa without changing the
    # global config.
    allowed_role_clusters: list[str] = Field(default_factory=list, max_length=20)
    # Additional platforms to exclude from the picker (extends the
    # always-excluded ``EXCLUDED_PLATFORMS = {"linkedin"}`` constant).
    # Use case: user has bad experience with a particular ATS and
    # wants the routine to skip it without dropping the platform
    # globally for everyone else.
    extra_excluded_platforms: list[str] = Field(default_factory=list, max_length=20)
    # F259: company-level exclude. Any job whose ``Job.company_id``
    # appears in this list is dropped from auto-picks regardless of
    # its score / cluster / geography. Companion to the per-job
    # ``routine_targets`` exclude (which is for "skip THIS specific
    # job"); this one is for "never apply to ANY job at this company"
    # — useful after a bad reviewer experience or a no-fly-list
    # decision. Capped at 200 entries to keep the IN-clause fast.
    excluded_company_ids: list[UUID] = Field(default_factory=list, max_length=200)
    # ── F360: server-side auto-apply ────────────────────────────────
    # Both default OFF. Unattended submission sends real applications to
    # real employers under the user's name and cannot be undone, so it
    # is opt-in per user and requires an explicit non-zero cap — there is
    # deliberately no "sensible default" that starts applying for
    # someone who never asked.
    auto_apply_enabled: bool = False
    # Ceiling on applications the sweeper may submit per rolling 24h.
    # 0 means auto-apply does nothing even when enabled.
    auto_apply_daily_cap: int = Field(default=0, ge=0, le=50)
    # Floor on Job.relevance_score for auto-apply specifically. Separate
    # from min_relevance_score (which filters what a human is *shown*)
    # because the bar for "send this unattended" should be higher than
    # the bar for "put this in front of me".
    auto_apply_min_score: int = Field(default=80, ge=0, le=100)


class RoutineTargetCreate(BaseModel):
    """Body for ``POST /routine/queue/{job_id}``."""
    model_config = ConfigDict(extra="forbid")

    intent: RoutineTargetIntent = "queued"
    note: str = Field(default="", max_length=500)


class RoutineTargetOut(BaseModel):
    """One row from ``GET /routine/queue``."""
    id: UUID
    job_id: UUID
    intent: RoutineTargetIntent
    note: str
    created_at: datetime
    updated_at: datetime
    # Job + company display fields hydrated by the handler so the UI
    # can render the row without a second round-trip per job.
    job_title: str = ""
    company_name: str = ""
    job_url: str = ""
    relevance_score: float = 0.0
    platform: str = ""

    model_config = ConfigDict(from_attributes=True)


class RoutineQueueResponse(BaseModel):
    queued: list[RoutineTargetOut]
    excluded: list[RoutineTargetOut]


class ExcludedCompany(BaseModel):
    """Hydrated row from ``GET /routine/excluded-companies`` —
    ``RoutinePreferences.excluded_company_ids`` joined with the
    ``companies`` table so the UI can render names + slugs without
    a second per-id round-trip."""
    id: UUID
    name: str
    slug: str = ""


class CreateRoutineRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: RoutineMode
    # Optional list of job_ids the routine will target this run.
    # We don't enforce that the routine actually attempts these —
    # it's advisory metadata so a historical run row can show
    # "you planned to apply to these 10 jobs."
    target_job_ids: list[UUID] | None = Field(default=None, max_length=50)
    # Optional idempotency key. When present and a run already exists
    # for ``(user_id, idempotency_key)``, the handler returns that
    # run's id instead of creating a new one — protects against
    # MCP-Chrome retrying a request whose response was lost in flight.
    # Clients should generate a UUID4 per logical attempt and retry
    # with the same key until they get a 2xx.
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)


class CreateRoutineRunResponse(BaseModel):
    run_id: UUID
    # True when the returned run_id was looked up by idempotency_key
    # rather than freshly created. Lets the client distinguish "my
    # retry succeeded" from "a new run started".
    replayed: bool = False


class UpdateRoutineRunRequest(BaseModel):
    """PATCH /routine/runs/{id} — incremental updates during a run.

    All fields optional so the routine can POST partial updates (e.g.
    just incrementing ``applications_submitted`` after a submit, or
    only setting ``status="complete"`` at the end).
    """

    model_config = ConfigDict(extra="forbid")

    applications_attempted: int | None = Field(default=None, ge=0)
    applications_submitted: int | None = Field(default=None, ge=0)
    applications_skipped: list[dict[str, Any]] | None = None
    detection_incidents: list[dict[str, Any]] | None = None
    status: RoutineStatus | None = None
    ended_at: datetime | None = None
    kill_switch_triggered: bool | None = None


class RoutineRunOut(BaseModel):
    id: UUID
    user_id: UUID
    started_at: datetime
    ended_at: datetime | None
    mode: RoutineMode
    applications_attempted: int
    applications_submitted: int
    applications_skipped: list[dict[str, Any]]
    detection_incidents: list[dict[str, Any]]
    status: RoutineStatus
    kill_switch_triggered: bool


class RoutineRunDetail(RoutineRunOut):
    """Same as RoutineRunOut + the submissions from this run."""

    submissions: list[SubmissionDetail]


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled: bool
    reason: str | None = Field(default=None, max_length=500)


class KillSwitchResponse(BaseModel):
    disabled: bool
    disabled_at: datetime | None
    reason: str | None


# ═══════════════════════════════════════════════════════════════════
# Humanize helper
# ═══════════════════════════════════════════════════════════════════

class HumanizeRequest(BaseModel):
    """Body of POST /routine/humanize.

    Runs the humanizer pipeline on arbitrary text — banned-phrase
    strip, burstiness check, style-match few-shot. The routine calls
    this after cover-letter generation and after any LLM answer
    generation. Separated from the content-generating endpoints so
    we can humanize text from any source (cover letter, generated
    answer, or a future LLM-drafted response) with one pipeline.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=20000)
    # Optional question context — lets the style-match pass pull
    # few-shot examples where the user has answered similar questions.
    # If null, style-match falls back to generic recent examples.
    question: str | None = Field(default=None, max_length=2000)


class HumanizeResponse(BaseModel):
    text: str  # final humanized output
    passes_applied: list[str]
    burstiness_sigma: float
    banned_phrase_hits: list[str]
    style_match_examples_used: int
