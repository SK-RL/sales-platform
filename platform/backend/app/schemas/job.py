from pydantic import BaseModel, ConfigDict, computed_field
from datetime import datetime
from typing import Literal
from uuid import UUID


# Regression finding 99: before this tuple existed, `BulkActionRequest.action`
# and `JobStatusUpdate.status` were typed `str` with a throwaway comment,
# which let an admin persist any garbage string (`"BOGUS_STATUS_XYZ"`,
# `"reset"`, typos). 25 prod rows already landed in a bogus `"reset"`
# state and quietly fell off `?status=new` queries, under-counting the
# review queue. Kept as a module-level tuple so the API layer and the
# cleanup script (`app.cleanup_job_status`) share one source of truth.
JOB_STATUS_VALUES = (
    "new",
    "under_review",
    "accepted",
    "rejected",
    "hidden",
    "archived",
)
JobStatusLiteral = Literal["new", "under_review", "accepted", "rejected", "hidden", "archived"]

# Regression finding 187 / 218: filter endpoints accept an extended status
# vocabulary that includes `expired` (terminal state for jobs that fell off
# the source board) in addition to the write-side `JobStatusLiteral` values.
# Two reasons they differ: (a) historical rows persisted under a legacy
# "expired" status predate the F99 vocabulary cleanup and still need to be
# filterable; (b) `hidden` is a user-driven workflow state (reviewer hid
# this row from their queue) that's written via JobStatusUpdate, while
# `expired` is set by the scan worker. The write and filter vocabularies
# will converge once the legacy "expired" rows are migrated to "archived".
# Filters live here (not in export.py) so jobs.py / export.py can both
# import — F218 is the case where diverging local definitions bit us.
JobStatusFilter = Literal[
    "new", "under_review", "accepted", "rejected", "expired", "archived"
]

# Regression finding 218: `geography_bucket` was a free-form `str` query
# param on `/jobs`, so a typo (`geography_bucket=global-remote` with a
# dash instead of underscore, `geography_bucket=USA_ONLY` mis-cased, etc.)
# silently filtered to total=0 instead of 422-ing with the allowed values.
# Values mirror the `Job.geography_bucket` column comment in models/job.py.
GeographyBucketFilter = Literal["global_remote", "usa_only", "uae_only"]

# d0e1f2g3h4i5: new ``remote_policy`` filter that replaces the legacy
# ``geography_bucket``. Same parse-time validation pattern.
RemotePolicyFilter = Literal[
    "worldwide",
    "country_restricted",
    "region_restricted",
    "hybrid",
    "onsite",
    "unknown",
]

# Regression finding 191 (moved here in F218 for reuse): the platform set
# is fixed in code (BaseFetcher subclasses under `app/fetchers/`), so a
# Literal catches typos like `platform=GREENHOUSE` (uppercase) at parse
# time. Was originally declared in `api/v1/platforms.py:24`; moved here
# to avoid duplicate Literals drifting as new fetchers are added. Both
# `platforms.py` and `jobs.py` import this one definition.
#
# F218 follow-up (same round): the initial Literal enumerated only the 10
# ATS fetchers documented in CLAUDE.md and missed the four aggregator
# fetchers that were added later (`linkedin`, `remoteok`, `remotive`,
# `weworkremotely`) — all of which DO write to `Job.platform` and have
# live rows (linkedin 1.6k, weworkremotely 391, remoteok 197, remotive 25).
# Shipping the narrow list would have 422'd `?platform=linkedin` on the
# frontend's real dropdown, breaking 2.2k rows of visible jobs. The source
# of truth is the `PLATFORM` class-attribute on every `BaseFetcher`
# subclass in `app/fetchers/` — keep this tuple aligned with that.
PlatformFilter = Literal[
    "greenhouse", "lever", "ashby", "workable", "bamboohr",
    "smartrecruiters", "jobvite", "recruitee", "wellfound", "himalayas",
    "linkedin", "remoteok", "remotive", "weworkremotely",
    # HN monthly "Who is hiring?" thread — aggregator (see
    # app/fetchers/hackernews.py). Filterable from the UI dropdown
    # so admins can segment "came from HN" vs "came from Greenhouse".
    "hackernews",
    # YC Work at a Startup (workatastartup.com) — two-stage
    # aggregator joining yc-oss batch dumps with WaaS job search.
    # See app/fetchers/yc_waas.py.
    "yc_waas",
    # F335 — Working Nomads (workingnomads.com) RSS aggregator.
    # Curated remote-jobs feed with strong infra/devops/SRE
    # cohort. Same shape as WWR/RemoteOK: single board with
    # slug='__all__'. See app/fetchers/workingnomads.py.
    "workingnomads",
]


class JobOut(BaseModel):
    id: UUID
    external_id: str
    company_id: UUID
    company_name: str | None = None
    title: str
    url: str
    platform: str
    location_raw: str
    remote_scope: str
    department: str
    employment_type: str | None = None
    salary_range: str | None = None
    geography_bucket: str
    # Remote-policy classification (d0e1f2g3h4i5). Replaces the
    # legacy ``geography_bucket`` for new code paths; both columns
    # are shadow-written by the classifier.
    remote_policy: str = "unknown"
    remote_policy_countries: list[str] = []
    matched_role: str
    role_cluster: str
    relevance_score: float
    status: str
    posted_at: datetime | None = None
    first_seen_at: datetime
    last_seen_at: datetime

    model_config = {"from_attributes": True}

    # Aliases to match frontend expectations
    @computed_field
    @property
    def source_platform(self) -> str:
        return self.platform

    @computed_field
    @property
    def location_restriction(self) -> str:
        return self.location_raw

    @computed_field
    @property
    def scraped_at(self) -> str:
        return self.first_seen_at.isoformat()

    @computed_field
    @property
    def created_at(self) -> str:
        return self.first_seen_at.isoformat()

    @computed_field
    @property
    def updated_at(self) -> str:
        return self.last_seen_at.isoformat()

    @computed_field
    @property
    def tags(self) -> list[str]:
        """Auto-generate tags from job metadata.

        Prefers the new ``remote_policy`` rendering when present —
        falls back to the legacy ``geography_bucket`` for old rows
        not yet re-classified by the maintenance task.
        """
        t = []
        if self.role_cluster:
            t.append(self.role_cluster)
        # New vocabulary first. ``country_restricted`` with a single
        # country reads as "US-restricted remote" rather than the
        # generic "Country-restricted remote" — matches what the
        # frontend ``renderRemotePolicyLabel`` helper produces.
        if self.remote_policy and self.remote_policy != "unknown":
            from app.utils.remote_policy import POLICY_LABELS
            if (
                self.remote_policy == "country_restricted"
                and self.remote_policy_countries
            ):
                codes = self.remote_policy_countries
                if len(codes) == 1:
                    t.append(f"{codes[0]}-restricted remote")
                elif len(codes) <= 2:
                    t.append(f"{'/'.join(codes)}-restricted remote")
                else:
                    t.append(f"{len(codes)} countries restricted")
            else:
                t.append(POLICY_LABELS.get(self.remote_policy, self.remote_policy))
        elif self.geography_bucket:
            t.append(self.geography_bucket.replace("_", " "))
        if self.matched_role and self.matched_role != self.title:
            t.append(self.matched_role)
        return t


class JobDescriptionOut(BaseModel):
    # Regression finding 168: `parsed_requirements`, `parsed_nice_to_have`,
    # and `parsed_tech_stack` shipped as `list[str] = []` but were never
    # populated — the handler hard-coded `[]` at both return sites and the
    # pipeline had no extraction step. The frontend rendered them behind
    # `length > 0` guards, so they never appeared on screen either. Removed
    # rather than stubbed so the API contract reflects what the server
    # actually produces. If bullet-point extraction lands later, add the
    # fields back together with the parser that populates them.
    id: UUID | None = None
    job_id: UUID | None = None
    raw_text: str = ""

    model_config = {"from_attributes": True}


class JobStatusUpdate(BaseModel):
    # F268 — extra="forbid" so callers can't sneak in unknown fields.
    # The bulk-action endpoint hits this schema; it's a high-leverage
    # admin path where typos in the UI payload should be visible.
    model_config = ConfigDict(extra="forbid")

    # Restricted to the documented status vocabulary (see JOB_STATUS_VALUES).
    # FastAPI returns a 422 with the allowed values if a client sends
    # something else — no more silent persistence of typos / unknown states.
    status: JobStatusLiteral


# Regression finding 69: the bulk endpoint used to accept only `job_ids:
# list[UUID]`, so "reject every job matching this filter" at 47k rows
# meant pulling 1,911 pages of 25-row results client-side and POSTing
# them back in the same shape — fragile, hard to audit, and each round
# trip re-competed for the tab's network budget. The filter-based
# branch lets callers send the same query params they'd send to
# `GET /jobs` + an `action` and let the server enumerate the id set.
# Either `job_ids` or `filter` must be present (but not both) — the
# handler validates that and rejects ambiguous requests up front.
#
# Safety caps (enforced in the handler, not here, so the cap is a
# single source of truth): the filter branch is capped at
# `BULK_FILTER_MAX` rows per request so a misclick on "Select all N
# matching" with no filters can't mass-mutate the entire 47k corpus
# in one keystroke. Callers hitting the cap get a 400 with the
# current matching count + the cap, so they can narrow before retry.
class BulkFilterCriteria(BaseModel):
    """Filter criteria for the filter-based bulk branch.

    Mirrors the subset of `GET /jobs` query params that are meaningful
    for a bulk action. Only fields the user *sees* in the table filter
    UI are exposed — `sort_by`/`sort_dir`/`page` are not, because they
    don't affect the matching id set. All fields are optional so the
    caller can send exactly the filters that were active when they
    clicked "Select all N matching"."""

    status: str | None = None
    platform: str | None = None
    geography_bucket: str | None = None
    role_cluster: str | None = None
    is_classified: bool | None = None
    search: str | None = None
    company_id: UUID | None = None


class BulkActionRequest(BaseModel):
    # At least one must be set (XOR enforced in the handler):
    job_ids: list[UUID] | None = None
    filter: BulkFilterCriteria | None = None
    action: JobStatusLiteral
