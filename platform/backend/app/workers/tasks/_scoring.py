"""Multi-signal relevance scoring for jobs."""

from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Source priority tiers (lower = better)
# ---------------------------------------------------------------------------

SOURCE_TIERS = {
    # Tier 1 -- original career pages and top ATS
    "greenhouse": 1,
    "lever": 1,
    "ashby": 1,
    "workable": 1,
    "bamboohr": 1,
    "career_page": 1,
    # Tier 2 -- established ATS platforms and job boards
    "smartrecruiters": 2,
    "jobvite": 2,
    "recruitee": 2,
    "breezy": 2,
    "personio": 2,
    "wellfound": 2,
    "linkedin": 2,
    "builtin": 2,
    # Tier 3 -- remote-focused aggregators
    "weworkremotely": 3,
    "remoteok": 3,
    "remotive": 3,
    "himalayas": 3,
    "indeed": 3,
    # HN "Who is hiring?" — tier 2 despite being an aggregator.
    # Cross-board quality check: HN hirers skew heavily toward
    # engineering-first / infra-heavy cos (dev-tools, startups with
    # strong technical hiring bars) rather than the broader remote
    # population that RemoteOK / WWR index. Tier 2 matches wellfound
    # / linkedin which have a similar quality profile.
    "hackernews": 2,
    # YC Work at a Startup — tier 2 also. YC cohorts are already
    # vetted for technical bar and tend to be the "who will be
    # hiring aggressively in 6 months" pool (small teams, post-
    # funding, growth mode). Higher quality signal per posting
    # than a generic remote aggregator.
    "yc_waas": 2,
}


def _title_match_score(matched_role: str, role_cluster: str, approved_roles_set: set[str] | None = None) -> float:
    """Score 0..1 for how well the title matches an approved role.

    - Exact approved role match: 1.0
    - Cluster-only keyword match (no exact role): 0.5
    - No match: 0.0
    """
    if not matched_role and not role_cluster:
        return 0.0
    if role_cluster and matched_role:
        if approved_roles_set is None:
            from app.workers.tasks._role_matching import INFRA_ROLES, SECURITY_ROLES, QA_ROLES
            approved_roles_set = {r.lower() for r in INFRA_ROLES + SECURITY_ROLES + QA_ROLES}
        if matched_role.lower() in approved_roles_set:
            return 1.0
        return 0.5
    return 0.0


def _company_fit_score(is_target: bool) -> float:
    """Score 0..1 for company fit. Target companies get 1.0, others 0.3."""
    return 1.0 if is_target else 0.3


def _geography_clarity_score(geography_bucket: str, remote_scope: str) -> float:
    """Score 0..1 for how clear the geography/remote scope is.

    - Known bucket + explicit scope: 1.0
    - Known bucket, sparse scope: 0.7
    - Unknown bucket: 0.2
    """
    if geography_bucket:
        if remote_scope and len(remote_scope.strip()) > 3:
            return 1.0
        return 0.7
    return 0.2


def _source_priority_score(platform: str) -> float:
    """Score 0..1 based on source tier."""
    tier = SOURCE_TIERS.get(platform.lower(), 3)
    if tier == 1:
        return 1.0
    elif tier == 2:
        return 0.6
    return 0.3


def _freshness_score(posted_at: datetime | None) -> float:
    """Score 0..1 based on how recently the job was posted.

    - Last 3 days: 1.0
    - Last 7 days: 0.8
    - Last 14 days: 0.5
    - Last 30 days: 0.3
    - Older / unknown: 0.1
    """
    if posted_at is None:
        return 0.1

    now = datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    age_days = (now - posted_at).days

    if age_days <= 3:
        return 1.0
    elif age_days <= 7:
        return 0.8
    elif age_days <= 14:
        return 0.5
    elif age_days <= 30:
        return 0.3
    return 0.1


def compute_relevance_score(
    title: str,
    matched_role: str,
    role_cluster: str,
    is_target: bool,
    geography_bucket: str,
    remote_scope: str,
    platform: str,
    posted_at: datetime | None,
    approved_roles_set: set[str] | None = None,
    feedback_adjustment: float = 0.0,
) -> float:
    """Compute a weighted relevance score between 0 and 100.

    Weights:
        Title match:       40%
        Company fit:       20%
        Geography clarity: 20%
        Source priority:   10%
        Freshness:         10%

    feedback_adjustment is added after base score (can be negative).

    Finding 86: jobs that fall outside every configured role cluster
    (no matched_role AND no role_cluster) get `relevance_score = 0`,
    honoring the documented contract in CLAUDE.md ("Jobs outside these
    clusters are saved but unscored"). Prior to the short-circuit the
    weighted sum still applied 60% of the weight to company/geo/source/
    freshness, giving unclassified jobs 14-54 and letting e.g. a
    "Talent Acquisition Coordinator" (score 44) outrank real security
    jobs with sub-50 scores in the global sort.
    """
    title_score = _title_match_score(matched_role, role_cluster, approved_roles_set)
    if title_score == 0.0:
        # Unclassified → zero. `feedback_adjustment` deliberately does
        # not apply here: if an operator wants to surface unclassified
        # jobs later, they should use a separate ranking signal rather
        # than leaking through the relevance-score contract.
        return 0.0
    score = (
        0.40 * title_score
        + 0.20 * _company_fit_score(is_target)
        + 0.20 * _geography_clarity_score(geography_bucket, remote_scope)
        + 0.10 * _source_priority_score(platform)
        + 0.10 * _freshness_score(posted_at)
    )
    adjusted = score * 100 + feedback_adjustment
    return round(max(0.0, min(100.0, adjusted)), 2)
