"""Job listing API endpoints."""

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from sqlalchemy import select, func, or_, literal, case, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.job import Job, JobDescription
from app.models.company import Company, CompanyATSBoard
from app.models.user import User
from app.models.resume import ResumeScore
from app.models.review import Review
from app.models.role_config import RoleClusterConfig
from app.models.audit_log import AuditLog
from app.api.deps import get_current_user, require_role
from app.schemas.job import (
    JobOut, JobDescriptionOut, JobStatusUpdate, BulkActionRequest,
    JobStatusFilter, GeographyBucketFilter, RemotePolicyFilter, PlatformFilter,
)
from app.utils.audit import log_action
from app.utils.sanitize import sanitize_html
from app.utils.sql import escape_like


async def _get_relevant_clusters(db: AsyncSession) -> list[str]:
    """Get the list of role cluster names that are marked as relevant."""
    result = await db.execute(
        select(RoleClusterConfig.name).where(
            RoleClusterConfig.is_relevant == True,
            RoleClusterConfig.is_active == True,
        )
    )
    clusters = result.scalars().all()
    # Fallback to hardcoded if no config exists yet
    return list(clusters) if clusters else ["infra", "security"]


async def _get_all_cluster_names(db: AsyncSession) -> list[str]:
    """Return every configured cluster name (active or not) for F218 filter
    validation.

    Mirrors the helper in export.py (F187). Clusters are admin-configurable
    via `RoleClusterConfig`, so we can't hard-code a `Literal` — we load
    the catalog at request time and reject unknown names with a 400 before
    they reach the SQL filter. The "relevant" pseudo-value is a UI alias
    for "all clusters marked relevant" and must be allow-listed explicitly
    (see F106 and the branching in `list_jobs` below).
    """
    result = await db.execute(select(RoleClusterConfig.name))
    return list(result.scalars().all())

router = APIRouter(prefix="/jobs", tags=["jobs"])


# Regression finding 198: `sort_by` was typed `str` and resolved via
# `getattr(Job, sort_by, None)` with a fallback to `first_seen_at`. But
# `getattr` happily returns relationships (`Job.reviews` → InstrumentedList
# proxy), JSON columns (`Job.raw_json` → "could not identify an equality
# operator" on ORDER BY), and private attrs (`_sa_instance_state` →
# CompileError). All three bubbled back as HTTP 500 "Internal Server
# Error" with a stack trace the frontend couldn't distinguish from a
# real crash.
#
# Fix: Literal-typed param so FastAPI 422s the bad sort at parse time,
# and a hand-maintained map to actual columns (no `getattr`) so we
# never accidentally orderable-ize something that isn't. `company_name`
# and `resume_score` stay keys because they drive the special JOIN
# branches below, but they don't appear in the column map — the if-chain
# consumes them before the map lookup.
_JOB_SORT_COLUMNS = {
    "first_seen_at":   Job.first_seen_at,
    "last_seen_at":    Job.last_seen_at,
    "relevance_score": Job.relevance_score,
    "posted_at":       Job.posted_at,
    "title":           Job.title,
    "status":          Job.status,
    # `platform` was advertised in the JobsPage sort dropdown but never
    # made it into the column map — picking "Platform A-Z" 422'd via
    # the F198 Literal guard (latent bug since F198 shipped). Adding
    # the column here + the matching Literal entry below + the
    # `_ALLOWED_SORT_KEYS` member makes the dropdown option work and
    # also unlocks `platform` as a multi-sort tiebreaker (e.g. group
    # infra jobs by platform with relevance as secondary).
    "platform":        Job.platform,
}

JobSortBy = Literal[
    "first_seen_at",
    "last_seen_at",
    "relevance_score",
    "posted_at",
    "title",
    "company_name",
    "resume_score",
    "status",
    "platform",
]
JobSortDir = Literal["asc", "desc"]

# Single source of truth for allowed sort keys — used by the multi-sort
# parser below. `JobSortBy` Literal stays for API-surface documentation
# and for the legacy single-sort path; the parser validates against this
# set directly because a runtime `sort_by` like
# `relevance_score:desc,first_seen_at:desc` is a single `str` arg and
# the per-segment keys aren't individually Literal-typeable at the
# signature.
_ALLOWED_SORT_KEYS = frozenset({
    "first_seen_at", "last_seen_at", "relevance_score",
    "posted_at", "title", "company_name", "resume_score", "status",
    "platform",
})


def _parse_sort_spec(sort_by: str, fallback_dir: str) -> list[tuple[str, str]]:
    """Parse a sort spec into a list of ``(key, dir)`` tuples.

    Accepts two forms:

    - **Legacy single-sort** — `sort_by="relevance_score"` (+ the separate
      `sort_dir` query param). Returns ``[(key, fallback_dir)]``.
    - **Multi-sort** — `sort_by="relevance_score:desc,first_seen_at:desc"`.
      The per-segment direction overrides `fallback_dir`. Segments
      without a `:` inherit `fallback_dir`.

    Raises ``HTTPException(422)`` on any unknown key or direction so
    callers get the same parse-time rejection as the Literal-typed
    single-sort path (F198). Deduplicates keys in-place — the first
    occurrence wins — so a shift-click chain that accidentally repeats
    a key still produces valid SQL.
    """
    segments = [s.strip() for s in sort_by.split(",") if s.strip()]
    if not segments:
        return [("first_seen_at", fallback_dir)]
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for seg in segments:
        if ":" in seg:
            key, _, direction = seg.partition(":")
            key = key.strip()
            direction = direction.strip().lower() or fallback_dir
        else:
            key = seg
            direction = fallback_dir
        if key not in _ALLOWED_SORT_KEYS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid sort_by key '{key}'. Must be one of: "
                    + ", ".join(sorted(_ALLOWED_SORT_KEYS))
                ),
            )
        if direction not in ("asc", "desc"):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid sort direction '{direction}' for '{key}'. Must be 'asc' or 'desc'.",
            )
        if key in seen:
            # User shift-clicked the same column twice — keep the first
            # (earlier in the chain = higher priority). Silently drop
            # the dupe instead of 422ing, since it's a UI artifact not
            # a wire-protocol violation.
            continue
        seen.add(key)
        parsed.append((key, direction))
    return parsed


@router.get("")
async def list_jobs(
    # Regression finding 218: these four filter params were previously typed
    # `str | None` with no validation — any typo (status=Accepted with a
    # capital A, geography_bucket=global-remote with a dash, role_cluster=
    # infraa) silently filtered to `total: 0` and the user saw "no matches"
    # for what looked like a valid filter. F187 shipped the same fix on the
    # parallel `/export/jobs` handler; this is the matching fix for the list
    # endpoint. `sort_by` was already Literal-typed per F198.
    #
    # Why Literal for status/geography but runtime validation for
    # role_cluster: status values are frozen in the code (models/job.py:33
    # documents them), so Literal gives us parse-time 422s for free.
    # `geography_bucket` is similarly frozen (models/job.py:25). But role
    # clusters are admin-configurable via RoleClusterConfig — a `Literal`
    # here would become stale the moment an admin adds a cluster — so we
    # load the catalog at request time and 400 on miss. Same split as the
    # export handler.
    status: JobStatusFilter | None = None,
    platform: PlatformFilter | None = None,
    source_platform: PlatformFilter | None = None,
    company_id: UUID | None = None,
    company: str | None = None,
    geography_bucket: GeographyBucketFilter | None = None,
    geography: GeographyBucketFilter | None = None,
    # d0e1f2g3h4i5: new vocabulary. Accepts ``remote_policy`` (enum)
    # plus optional ``remote_country`` (single ISO-3166 alpha-2 code,
    # uppercased; matches against the JSONB containment ``@>``).
    # Both legacy (``geography``) and new (``remote_policy``) filters
    # are supported in parallel during the transition window.
    remote_policy: RemotePolicyFilter | None = None,
    remote_country: str | None = None,
    role_cluster: str | None = None,
    is_classified: bool | None = None,
    search: str | None = None,
    q: str | None = None,
    # F198: Literal-typed → FastAPI 422s unknown sort keys at parse
    # time instead of 500-ing from the `getattr` path below.
    #
    # Multi-sort extension (Apr 2026): when `sort_by` contains a `,` or
    # `:` it is parsed as a comma-separated list of `key:dir` pairs,
    # e.g. `relevance_score:desc,first_seen_at:desc`. The per-segment
    # direction overrides `sort_dir`. Each key is validated against
    # `_ALLOWED_SORT_KEYS` at request time (FastAPI's Literal can't
    # validate the inside of a comma-separated string, so we parse
    # ourselves and 422 with a key list on miss). The legacy single
    # `sort_by=relevance_score` + `sort_dir=desc` form remains the
    # default and works unchanged for callers that pre-date multi-sort.
    sort_by: str = "first_seen_at",
    sort_dir: JobSortDir = "desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    page_size: int | None = Query(None, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Accept page_size as alias for per_page (frontend sends page_size)
    if page_size is not None:
        per_page = page_size

    # Regression finding 33: the response schema aliases `Job.platform` as
    # `source_platform` and field-level aliases for `search` (`q`) and
    # `company_id` (`company`) are expected by callers who are reading the
    # response field names and assuming the query params match. Accept the
    # aliases here as a non-breaking alternative to the original param
    # names — callers who were passing the original names still work.
    effective_platform = platform or source_platform
    effective_search = search or q

    query = select(Job).options(joinedload(Job.company))

    if status:
        query = query.where(Job.status == status)
    if effective_platform:
        query = query.where(Job.platform == effective_platform)
    if company_id:
        query = query.where(Job.company_id == company_id)
    if company and company.strip():
        # `company=` is a name-substring filter (the id-based filter lives on
        # `company_id`). Matches the same ilike pattern used for the combined
        # `search` box so the two behave consistently. Findings 84+85:
        # escape LIKE metachars and strip whitespace-only input so `"100%"`
        # / `"dev_ops"` / `"   "` don't degenerate into wildcard matches.
        needle = f"%{escape_like(company.strip())}%"
        query = query.where(Job.company.has(Company.name.ilike(needle, escape="\\")))
    geo = geography_bucket or geography
    if geo:
        query = query.where(Job.geography_bucket == geo)
    if remote_policy:
        query = query.where(Job.remote_policy == remote_policy)
    if remote_country:
        # JSONB containment — the Postgres ``@>`` operator. Manual
        # testing on a fresh local DB caught two SA-level traps:
        #   * ``.op("@>")(string_literal)`` binds the literal as
        #     VARCHAR → ``operator does not exist: jsonb @>
        #     character varying``.
        #   * ``cast(literal, JSONB)`` doesn't change the bound
        #     parameter's type — only how the literal is rendered
        #     in the SQL string. asyncpg still ships VARCHAR.
        #   * ``.contains([code])`` on a JSONB-typed column has
        #     the same outcome — the right-side type isn't
        #     inferred from the column.
        # The reliable path is an explicit ``bindparam`` with
        # ``type_=JSONB``: the binding tells the driver to send
        # the value as a JSONB-encoded string, so the operator
        # has matching types on both sides and the GIN index is
        # used.
        from app.utils.remote_policy import normalise_country
        from sqlalchemy import bindparam
        from sqlalchemy.dialects.postgresql import JSONB
        try:
            code = normalise_country(remote_country)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        query = query.where(
            Job.remote_policy_countries.op("@>")(
                bindparam("remote_country_filter", value=[code], type_=JSONB)
            )
        )
    if role_cluster:
        # F260 regression fix: ``role_cluster=any`` is the explicit
        # "no filter" sentinel introduced for the Sidebar's "All Jobs"
        # link (feedback fc0a750b — pre-fix that link had no query
        # params, fell into JobsPage's localStorage-restore branch,
        # silently re-applied any prior ``role_cluster=relevant``
        # filter, and made "All Jobs" indistinguishable from "Relevant
        # Jobs"). Treating ``any`` as a no-op keeps the wire shape
        # of the request explicit while preserving the legacy
        # behaviour of an empty-string ``role_cluster`` value.
        if role_cluster == "any":
            pass  # Explicit "no role-cluster filter" — fall through.
        else:
            # F218: validate role_cluster against the configured cluster
            # catalog before filtering. Clusters are admin-configurable, so
            # we load the allowed set from the DB instead of hard-coding a
            # Literal. The "relevant" pseudo-value is a UI alias for "all
            # clusters marked relevant" (F106) and must be allow-listed
            # explicitly. Before this check, role_cluster=infraa silently
            # returned total=0; now it 400s with the catalog in the detail
            # so the caller can self-correct.
            allowed_clusters = set(await _get_all_cluster_names(db)) | {"relevant", "any"}
            if role_cluster not in allowed_clusters:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invalid role_cluster. Must be one of: "
                        + ", ".join(sorted(allowed_clusters))
                    ),
                )
            if role_cluster == "relevant":
                relevant_clusters = await _get_relevant_clusters(db)
                query = query.where(Job.role_cluster.in_(relevant_clusters))
            else:
                query = query.where(Job.role_cluster == role_cluster)

    # Regression finding 87: let callers filter the (huge — ~90% of rows)
    # unclassified pool without hand-crafting `role_cluster=` URLs that
    # some clients strip as empty. `is_classified=false` → the row has
    # NULL or "" cluster; `is_classified=true` → anything else. We
    # check both NULL and "" because historical rows use the empty
    # string while newer inserts may leave it NULL. Combining this with
    # `role_cluster=foo` is contradictory-but-valid SQL (returns 0) —
    # we don't reject it; the frontend just shouldn't send both.
    if is_classified is True:
        query = query.where(
            Job.role_cluster.is_not(None),
            Job.role_cluster != "",
        )
    elif is_classified is False:
        query = query.where(
            or_(Job.role_cluster.is_(None), Job.role_cluster == "")
        )

    if effective_search and effective_search.strip():
        # F240 (khushi.jain feedback "Search Bar Query"): boolean
        # syntax support. The user's query is detected as boolean
        # (presence of AND/OR/NOT operators, "quoted phrases", or
        # leading-minus exclusions) and then parsed + compiled into
        # a structured WHERE clause. Bare queries (no operators) fall
        # through to the legacy single-substring branch so existing
        # users see no change.
        #
        # Implicit AND between adjacent terms matches Google-style
        # search expectations: `cloud kubernetes` = both must match.
        # See app/utils/search_query.py for the full grammar.
        from app.utils.search_query import (
            SearchQueryError, compile_to_clause, is_boolean_query, parse,
            term_clause_factory,
        )
        search_str = effective_search.strip()

        if is_boolean_query(search_str):
            # Boolean path: each term still matches if ANY of (title,
            # company name, location_raw) contains it (preserves the
            # legacy per-term semantics — boolean ops compose ON TOP
            # of those per-term ANY-of-columns matches).
            #
            # `Company.name` lives on a related table; we need a
            # column-level Comparable that the parser can hand to
            # ILIKE. The Job.company.has(Company.name.ilike(...))
            # pattern in the legacy branch wraps the whole expression
            # in EXISTS — clean for one term but composes badly under
            # boolean ops (NOT EXISTS for NOT can be expensive). For
            # the parser path, use an explicit join + Company.name
            # column so the boolean composition stays a single SELECT.
            from sqlalchemy.orm import contains_eager
            query = query.join(Company, Job.company_id == Company.id)
            term_to_clause = term_clause_factory(
                Job.title, Company.name, Job.location_raw,
            )
            try:
                ast = parse(search_str)
            except SearchQueryError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Search syntax error: {e}",
                )
            query = query.where(compile_to_clause(ast, term_to_clause))
        else:
            # Legacy single-substring path. Findings 84+85: strip
            # whitespace-only input and escape LIKE metachars — a
            # search for `"100%"` must not match every row, and a
            # 3-space input must not wildcard-match random titles
            # with triple spaces.
            needle = f"%{escape_like(search_str)}%"
            # F276 — UNION-of-IDs rewrite. The previous shape was
            # ``or_(Job.title.ilike, Job.company.has(Company.name.ilike),
            # Job.location_raw.ilike)`` which the Postgres planner can't
            # split across multiple indexes: the OR forces a Hash Join
            # over a seq-scanned jobs table (114ms on 86k rows even
            # WITH the F274 + F275 trigram indexes in place — they
            # never fired because the planner can't pick "use index A
            # for branch 1 OR index B for branch 2" inside one SELECT).
            #
            # Rewriting as ``Job.id IN (UNION OF per-column SELECTs)``
            # gives each branch its own independent SELECT, so the
            # planner picks the right trigram index per branch:
            #   - title-branch  → idx_jobs_title_trgm (F274)
            #   - company-branch → idx_companies_name_trgm (F275)
            #   - location-branch → seq scan on (small) location_raw,
            #     no index but it's the cheapest of the three anyway.
            # EXPLAIN before:  Seq Scan on jobs (114ms)
            # EXPLAIN after:   Bitmap Index Scan x2 + UNION (~6ms).
            #
            # ``Company.name`` join lives ONLY inside the company
            # subquery — the outer query stays clean of the join,
            # avoiding any unintended cross-product with the rest of
            # the WHERE clause (geography filters, role_cluster, etc.
            # already compose against the unjoined Job alias).
            title_match = (
                select(Job.id)
                .where(Job.title.ilike(needle, escape="\\"))
            )
            company_match = (
                select(Job.id)
                .join(Company, Job.company_id == Company.id)
                .where(Company.name.ilike(needle, escape="\\"))
            )
            location_match = (
                select(Job.id)
                .where(Job.location_raw.ilike(needle, escape="\\"))
            )
            matching_ids = title_match.union(
                company_match, location_match,
            ).subquery()
            query = query.where(
                Job.id.in_(select(matching_ids.c.id))
            )

    # Count
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Parse multi-sort spec (single-sort is just a 1-element chain).
    # `_parse_sort_spec` validates each key + direction and 422s on miss.
    sort_chain = _parse_sort_spec(sort_by, sort_dir)

    # Resolve each (key, dir) into an SQLAlchemy ORDER BY expression,
    # joining once per dependent table. The original logic only had to
    # consider one `sort_by` value at a time; with multi-sort we may
    # need joins for both `resume_score` and `company_name` in the
    # same query, so we track which joins are already attached and
    # apply each at most once.
    joined_company = False
    joined_resume_scores = False
    order_clauses = []
    for key, direction in sort_chain:
        if key == "resume_score" and user.active_resume_id:
            if not joined_resume_scores:
                query = query.outerjoin(
                    ResumeScore,
                    (ResumeScore.job_id == Job.id)
                    & (ResumeScore.resume_id == user.active_resume_id),
                )
                joined_resume_scores = True
            score_col = func.coalesce(ResumeScore.overall_score, 0)
            order_clauses.append(score_col.desc() if direction == "desc" else score_col.asc())
        elif key == "resume_score":
            # No active resume — silently drop this sort key rather than
            # 422-ing, so a user removing their active resume doesn't
            # break their saved sort chain. Skipping degrades gracefully
            # to whatever other sort keys they had.
            continue
        elif key == "company_name":
            if not joined_company:
                query = query.join(Company, Job.company_id == Company.id)
                joined_company = True
            order_clauses.append(Company.name.desc() if direction == "desc" else Company.name.asc())
        else:
            # F198: hard-mapped lookup. Validation above guarantees the
            # key is in `_ALLOWED_SORT_KEYS`; the .get() fallback is
            # defense-in-depth for the case where a future allowed key
            # is added but the column map isn't updated.
            sort_col = _JOB_SORT_COLUMNS.get(key, Job.first_seen_at)
            order_clauses.append(sort_col.desc() if direction == "desc" else sort_col.asc())

    if not order_clauses:
        # Pathological case: chain was `resume_score` only and user has
        # no active resume → fell through with nothing. Fall back to
        # the documented default so the result set isn't database-
        # implementation-defined order.
        order_clauses = [Job.first_seen_at.desc()]

    # F271 — append ``Job.id`` as a final tiebreaker so pagination is
    # deterministic across requests. Without this, a sort like
    # ``relevance_score:desc`` produces unstable order on ties (many
    # jobs share score 93.16, 80.0, etc.) and a reviewer flipping
    # between pages can see the same row appear twice or vanish
    # because Postgres returns the tie-rows in implementation-
    # defined order. Adding Job.id (UUID, btree-indexed) costs
    # nothing on the planner but makes ORDER BY total — every tie
    # resolves consistently from one request to the next.
    # Skipped if the user already explicitly sorted by Job.id (avoid
    # ``ORDER BY id DESC, id ASC`` which Postgres simplifies to the
    # first one but is noisy in EXPLAIN).
    explicit_id_sort = any(
        getattr(c, "_F271_id_sentinel__", False) is True for c in order_clauses
    ) or any(
        # Pragmatic check via repr — sort_chain entries with key=="id"
        # would land here. Cheap; the chain is bounded by validation.
        ("jobs.id" in str(c).lower()) for c in order_clauses
    )
    if not explicit_id_sort:
        order_clauses.append(Job.id.asc())

    query = query.order_by(*order_clauses)

    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    jobs = result.unique().scalars().all()

    # Fetch resume scores for these jobs
    resume_scores_map: dict[str, float] = {}
    if user.active_resume_id and jobs:
        job_ids = [j.id for j in jobs]
        score_result = await db.execute(
            select(ResumeScore.job_id, ResumeScore.overall_score)
            .where(
                ResumeScore.resume_id == user.active_resume_id,
                ResumeScore.job_id.in_(job_ids),
            )
        )
        for job_id, score in score_result:
            resume_scores_map[str(job_id)] = round(score, 1)

    items = []
    for j in jobs:
        item = JobOut.model_validate(j)
        item.company_name = j.company.name if j.company else None
        items.append(item)

    # Serialize with resume_score enrichment
    items_data = []
    for item, j in zip(items, jobs):
        d = item.model_dump(mode="json")
        d["resume_score"] = resume_scores_map.get(str(j.id))
        items_data.append(d)

    return {"items": items_data, "total": total, "page": page, "page_size": per_page, "total_pages": (total + per_page - 1) // per_page}


@router.get("/review-queue")
async def review_queue(
    limit: int = Query(20, ge=1, le=100),
    # F294 (closes F230): pre-fix the handler declared only ``limit``
    # and silently dropped every other query param. Adding the
    # filter axes (role_cluster, platform) closes F230(a). Adding
    # ``page`` + applying it to the SQL offset closes F230(b) — the
    # response envelope used to advertise ``page=1, total_pages=N``
    # but the handler never read ``?page=`` so ``?page=2`` returned
    # the same 20 items. Now genuinely paginates.
    page: int = Query(1, ge=1),
    role_cluster: str | None = None,
    platform: str | None = None,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    """Return the next batch of unreviewed jobs, prioritized by discovery
    recency → CURRENT REVIEWER'S resume fit → platform relevance.

    Ordering (each tier DESC, NULLS LAST):
      1. ``DATE(first_seen_at)``    — today first, then yesterday, then older.
         Bucketing by calendar date (not timestamp) so the UI can surface a
         single "12 today / 8 yesterday" summary that matches the order.
      2. ``ResumeScore.overall_score`` for **the reviewer's own active
         resume**. Jobs the reviewer's resume hasn't been scored against
         yet fall to the bottom of this tier (coalesced to -1).
      3. ``Job.relevance_score``     — platform-wide role/geography match.

    Pre-fix this used ``MAX(ResumeScore.overall_score)`` across **every
    resume on the platform**. Real-world bug: a reviewer with a DevOps/SRE
    resume would see Data-Engineer / Automation-Engineer roles ranked at
    the top of their queue because some other team-mate's data-engineer
    resume scored those jobs at 90+. The team-wide max blew Sarthak's
    own ranking out of the water for any job a different specialist
    happened to fit. The right semantics for a per-reviewer queue is
    per-reviewer scoring — anyone else's fit is irrelevant to what
    *this* reviewer should triage next.

    If the reviewer has no active resume (``user.active_resume_id`` is
    NULL), the subquery returns no matching rows, every job's
    ``my_score`` coalesces to -1, and tier 2 collapses — ordering then
    falls back to ``Job.relevance_score`` which is correct for
    resume-less admin browsing.

    Also excludes jobs the *current reviewer* has already decided on
    (NOT EXISTS against reviews.reviewer_id), which is defense-in-depth
    against any future flow where a review is recorded without flipping
    ``Job.status`` off ``"new"``.
    """
    # Per-user resume-score subquery: only this reviewer's active resume.
    # Filtering on ``ResumeScore.resume_id == user.active_resume_id`` gives
    # us at most one row per job so no GROUP BY needed. ``literal(None)``
    # for the `where` LHS when active_resume_id is NULL is intentional —
    # SQL's ``x = NULL`` is always false so the subquery returns empty,
    # which is exactly what we want (the coalesce in ORDER BY handles it).
    my_score_sq = (
        select(
            ResumeScore.job_id.label("rs_job_id"),
            ResumeScore.overall_score.label("my_score"),
        )
        .where(ResumeScore.resume_id == user.active_resume_id)
        .subquery()
    )

    # Today/yesterday are computed in UTC — matches how first_seen_at is
    # stamped (scan_task uses datetime.now(timezone.utc)). A reviewer in
    # a non-UTC timezone may see a job stamped "today" that rolled over
    # locally, but the bucket boundaries are the same for every user and
    # every scan — consistency wins over per-user TZ conversion here.
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    reviewer_decided = exists().where(
        Review.job_id == Job.id,
        Review.reviewer_id == user.id,
    )

    # F294: build the WHERE chain once and reuse for both the
    # main query and the stats so they reconcile under the same
    # filter scope. Pre-fix stats counted the unfiltered table
    # while the items list (theoretically) could have been filtered
    # — the new filter params would have produced misleading stats
    # without this shared shape.
    base_filters = [Job.status == "new", ~reviewer_decided]
    if role_cluster:
        base_filters.append(Job.role_cluster == role_cluster)
    if platform:
        base_filters.append(Job.platform == platform)

    query = (
        select(Job, my_score_sq.c.my_score)
        .options(joinedload(Job.company))
        .outerjoin(my_score_sq, Job.id == my_score_sq.c.rs_job_id)
        .where(*base_filters)
        .order_by(
            func.date(Job.first_seen_at).desc(),
            func.coalesce(my_score_sq.c.my_score, literal(-1.0)).desc(),
            Job.relevance_score.desc(),
            Job.id.asc(),  # F271 stable-pagination tiebreaker
        )
        .offset((page - 1) * limit)
        .limit(limit)
    )
    result = await db.execute(query)
    rows = result.unique().all()

    # Queue stats — tile counts by discovery-date bucket so the UI can
    # render "12 today · 8 yesterday · 47 older" chips alongside the
    # prioritized list. F294: stats use the SAME filter chain as the
    # items list so the numbers reconcile 1:1 with what's actually
    # being surfaced under the current role_cluster/platform filter.
    stats_q = select(
        func.count(Job.id).label("total"),
        func.sum(case((func.date(Job.first_seen_at) == today, 1), else_=0)).label("today_count"),
        func.sum(case((func.date(Job.first_seen_at) == yesterday, 1), else_=0)).label("yesterday_count"),
        func.sum(case((func.date(Job.first_seen_at) < yesterday, 1), else_=0)).label("older_count"),
    ).where(*base_filters)
    stats_row = (await db.execute(stats_q)).one()

    items = []
    for j, my_score in rows:
        item = JobOut.model_validate(j)
        item.company_name = j.company.name if j.company else None
        d = item.model_dump(mode="json")
        # Per-reviewer resume score (active resume's fit for this job).
        # Keep ``max_resume_score`` populated with the same value for
        # backward-compat with any consumer / tab that hadn't been
        # updated yet — it's the same number from the reviewer's POV
        # since their queue is now per-user-scoped. The new
        # ``your_resume_score`` key is the canonical name going forward.
        rounded = round(my_score, 1) if my_score is not None else None
        d["your_resume_score"] = rounded
        d["max_resume_score"] = rounded
        items.append(d)

    total = int(stats_row.total or 0)
    # F294: total_pages reflects the actual paginate-able shape now
    # that ``page`` is honoured. ``ceil(total / limit)`` with a
    # minimum of 1 so an empty queue still renders sensibly.
    total_pages = 1 if total == 0 else (total + limit - 1) // limit
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": limit,
        "total_pages": total_pages,
        "stats": {
            "today": int(stats_row.today_count or 0),
            "yesterday": int(stats_row.yesterday_count or 0),
            "older": int(stats_row.older_count or 0),
            "total": total,
        },
    }


@router.get("/{job_id}")
async def get_job(job_id: UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Job).options(joinedload(Job.company), joinedload(Job.description)).where(Job.id == job_id)
    )
    job = result.unique().scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    data = JobOut.model_validate(job)
    data.company_name = job.company.name if job.company else None

    d = data.model_dump(mode="json")

    # Regression finding 97/101: surface whether this job has a
    # populated `JobDescription` row so the UI can render a "limited
    # data" badge when it doesn't. Pre-fix the resume scorer collapsed
    # to the cluster-baseline keyword bag for empty rows, producing
    # 4 distinct scores across 600+ jobs with no UI signal that the
    # underlying data was missing. The backfill task
    # (`maintenance_task.backfill_job_descriptions`) closes the gap
    # for historical rows; this field gives the UI a live signal so
    # users can interpret a low score as "JD is sparse" rather than
    # "this job is a poor match".
    has_description = bool(
        job.description
        and (job.description.text_content or job.description.html_content)
    )
    d["has_description"] = has_description

    # Enrich with resume score if active resume exists
    if user.active_resume_id:
        # The resume_scores table has no uniqueness constraint on
        # (resume_id, job_id), and in production we routinely end up
        # with 2+ rows for the same pair because the scoring task can
        # be scheduled multiple times (rescore_all_active_resumes +
        # per-upload score + manual rescore). `scalar_one_or_none()`
        # raises `MultipleResultsFound` on those rows, which bubbles
        # out of the handler as a 500 — that's the Senior Security
        # Engineer @ Bitwarden "Couldn't load this job" bug users
        # hit in prod this afternoon. Short-term fix: select the most
        # recently computed row and move on. The cleanup is a
        # follow-up: dedupe the rows + add a `UNIQUE (resume_id,
        # job_id)` constraint so the score-write path uses an
        # ON CONFLICT DO UPDATE. `/jobs` (list) never surfaced this
        # because it maps duplicates via dict last-write-wins — only
        # the single-job handler's `scalar_one_or_none` contract was
        # strict enough to raise.
        score_result = await db.execute(
            select(ResumeScore)
            .where(
                ResumeScore.resume_id == user.active_resume_id,
                ResumeScore.job_id == job.id,
            )
            .order_by(ResumeScore.scored_at.desc())
            .limit(1)
        )
        rs = score_result.scalar_one_or_none()
        if rs:
            d["resume_score"] = round(rs.overall_score, 1)
            d["resume_fit"] = {
                "overall_score": round(rs.overall_score, 1),
                "keyword_score": round(rs.keyword_score, 1),
                "role_match_score": round(rs.role_match_score, 1),
                "format_score": round(rs.format_score, 1),
                "matched_keywords": rs.matched_keywords or [],
                "missing_keywords": rs.missing_keywords or [],
                "suggestions": rs.suggestions or [],
            }
        else:
            d["resume_score"] = None
            d["resume_fit"] = None
    else:
        d["resume_score"] = None
        d["resume_fit"] = None

    return d


@router.get("/{job_id}/score-breakdown")
async def get_job_score_breakdown(job_id: UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Get scoring breakdown for a specific job."""
    result = await db.execute(
        select(Job).options(joinedload(Job.company)).where(Job.id == job_id)
    )
    job = result.unique().scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    from app.workers.tasks._scoring import (
        _title_match_score, _company_fit_score, _geography_clarity_score,
        _source_priority_score, _freshness_score,
    )
    from app.workers.tasks._role_matching import match_role

    role_match = match_role(job.title)
    title_score = _title_match_score(role_match["matched_role"], role_match["role_cluster"])
    company_score = _company_fit_score(job.company.is_target if job.company else False)
    geo_score = _geography_clarity_score(job.geography_bucket, job.remote_scope)
    source_score = _source_priority_score(job.platform)
    fresh_score = _freshness_score(job.posted_at)

    return {
        "total": job.relevance_score,
        "breakdown": [
            {"signal": "Title Match", "weight": 0.40, "raw": round(title_score, 2), "weighted": round(title_score * 0.40 * 100, 1), "detail": f"Matched: {role_match['matched_role'] or 'None'} ({role_match['role_cluster'] or 'no cluster'})"},
            {"signal": "Company Fit", "weight": 0.20, "raw": round(company_score, 2), "weighted": round(company_score * 0.20 * 100, 1), "detail": f"Target: {'Yes' if (job.company and job.company.is_target) else 'No'}"},
            {"signal": "Geography Clarity", "weight": 0.20, "raw": round(geo_score, 2), "weighted": round(geo_score * 0.20 * 100, 1), "detail": f"Bucket: {job.geography_bucket or 'unknown'}, Scope: {job.remote_scope or 'none'}"},
            {"signal": "Source Priority", "weight": 0.10, "raw": round(source_score, 2), "weighted": round(source_score * 0.10 * 100, 1), "detail": f"Platform: {job.platform}"},
            {"signal": "Freshness", "weight": 0.10, "raw": round(fresh_score, 2), "weighted": round(fresh_score * 0.10 * 100, 1), "detail": f"Posted: {job.posted_at.strftime('%Y-%m-%d') if job.posted_at else 'Unknown'}"},
        ],
    }


@router.get("/{job_id}/description")
async def get_job_description(job_id: UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(JobDescription).where(JobDescription.job_id == job_id))
    jd = result.scalar_one_or_none()
    if jd:
        # The frontend renders this via dangerouslySetInnerHTML, so any HTML
        # coming from ATS boards must be sanitized (strip <script>, event
        # handlers, javascript: URLs, etc.) before we hand it back.
        return JobDescriptionOut(
            id=jd.id,
            job_id=jd.job_id,
            raw_text=sanitize_html(jd.text_content or jd.html_content or ""),
        )

    # F291 (closes F122): the inline raw_json fallback used to live
    # right here and had drifted from the shared
    # ``app.utils.job_description.extract_description`` helper that
    # the scan pipeline + ``maintenance_task.backfill_job_descriptions``
    # use. The drift meant: smartrecruiters jobs (with structured
    # ``jobAd.sections``) returned empty; workable's ``full_description``
    # was missed; bamboohr's ``jobOpeningDescription`` was missed;
    # career-page rows didn't render. Single source of truth now —
    # the helper handles per-platform key mapping + the structured
    # sections branches that the inline fallback never knew about.
    from app.utils.job_description import extract_description

    job_result = await db.execute(select(Job).where(Job.id == job_id))
    job = job_result.scalar_one_or_none()
    raw_text = ""
    if job and job.raw_json:
        raw = job.raw_json if isinstance(job.raw_json, dict) else {}
        html_content, text_content = extract_description(
            job.platform or "", raw,
        )
        # Prefer the rendered text. Both come back already
        # entity-unescaped; the sanitize_html below strips dangerous
        # tags before the frontend's dangerouslySetInnerHTML render.
        raw_text = text_content or html_content or ""

    # Sanitize before returning — frontend renders via dangerouslySetInnerHTML.
    return JobDescriptionOut(raw_text=sanitize_html(raw_text))


@router.get("/{job_id}/reviews")
async def get_job_reviews(job_id: UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Get reviews for a specific job."""
    from app.models.review import Review
    from app.schemas.review import ReviewOut
    result = await db.execute(
        select(Review).options(joinedload(Review.reviewer))
        .where(Review.job_id == job_id)
        .order_by(Review.created_at.desc())
    )
    reviews = result.unique().scalars().all()
    items = []
    for r in reviews:
        item = ReviewOut.model_validate(r)
        item.reviewer_name = r.reviewer.name if r.reviewer else None
        items.append(item)
    return items


@router.patch("/{job_id}")
async def update_job_status(
    job_id: UUID, body: JobStatusUpdate,
    request: Request,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    old_status = job.status
    job.status = body.status
    await db.commit()

    await log_action(
        db, user,
        action="job.status_change",
        resource="job",
        request=request,
        metadata={"job_id": str(job_id), "old_status": old_status, "new_status": body.status},
    )

    return {"ok": True}


# Regression finding 69: hard cap on the filter branch. 47k-row corpora
# are realistic, so a "Select all N matching" click on no filter could
# trivially flip the status of every row in the DB. We reject requests
# whose filter matches > BULK_FILTER_MAX rows with a 400 that tells
# the caller the current count, so they can narrow before retry. The
# cap is deliberately generous (5000) — enough for a sales team to
# reject an entire source platform in one go, but small enough that
# a misclick can't corrupt the full corpus without visible friction.
BULK_FILTER_MAX = 5000


async def _build_bulk_filter_query(
    criteria, db: AsyncSession
):
    """Rebuild the same WHERE chain that `GET /jobs` uses so the filter
    branch and the list endpoint agree on which rows match — otherwise
    the "Select all 47,776 matching" count displayed in the UI could
    diverge from the set that actually gets updated, which would be
    exactly the kind of silent-blast-radius bug we're trying to
    prevent. Returns a select(Job) that the caller can execute or
    wrap in a count. Validates `role_cluster` against the configured
    catalog (same contract as `/jobs` F218) so a typo 400s here too
    instead of quietly updating zero rows."""
    query = select(Job)

    if criteria.status:
        query = query.where(Job.status == criteria.status)
    if criteria.platform:
        query = query.where(Job.platform == criteria.platform)
    if criteria.company_id:
        query = query.where(Job.company_id == criteria.company_id)
    if criteria.geography_bucket:
        query = query.where(Job.geography_bucket == criteria.geography_bucket)
    if criteria.role_cluster:
        allowed_clusters = set(await _get_all_cluster_names(db)) | {"relevant"}
        if criteria.role_cluster not in allowed_clusters:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid role_cluster. Must be one of: "
                    + ", ".join(sorted(allowed_clusters))
                ),
            )
        if criteria.role_cluster == "relevant":
            relevant_clusters = await _get_relevant_clusters(db)
            query = query.where(Job.role_cluster.in_(relevant_clusters))
        else:
            query = query.where(Job.role_cluster == criteria.role_cluster)
    if criteria.is_classified is True:
        query = query.where(Job.role_cluster.is_not(None), Job.role_cluster != "")
    elif criteria.is_classified is False:
        query = query.where(or_(Job.role_cluster.is_(None), Job.role_cluster == ""))
    if criteria.search and criteria.search.strip():
        needle = f"%{escape_like(criteria.search.strip())}%"
        # F309 (parallel to F276): apply the SAME UNION-of-IDs
        # rewrite that the main /jobs search uses. Pre-fix this
        # bulk-filter path used the OR-with-EXISTS shape that
        # forces a seq scan on jobs even with the F274/F275
        # trigram indexes in place. The bulk-action handler runs
        # this filter for the admin's "bulk-update everything in
        # the relevant cluster matching <term>" workflow — at
        # 86k jobs the seq scan adds real wall-clock to a
        # destination-state-changing endpoint that should be
        # snappy. Same EXPLAIN-before/after numbers as F276:
        # 114ms → ~6ms.
        title_match = (
            select(Job.id)
            .where(Job.title.ilike(needle, escape="\\"))
        )
        company_match = (
            select(Job.id)
            .join(Company, Job.company_id == Company.id)
            .where(Company.name.ilike(needle, escape="\\"))
        )
        location_match = (
            select(Job.id)
            .where(Job.location_raw.ilike(needle, escape="\\"))
        )
        matching_ids = title_match.union(
            company_match, location_match,
        ).subquery()
        query = query.where(
            Job.id.in_(select(matching_ids.c.id))
        )
    return query


@router.post("/bulk-action")
async def bulk_action(
    body: BulkActionRequest,
    request: Request,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    # Regression finding 69: two mutually-exclusive input shapes.
    # Reject requests that provide neither or both — ambiguous input
    # shouldn't silently prefer one path.
    if body.job_ids is None and body.filter is None:
        raise HTTPException(
            status_code=400,
            detail="Either `job_ids` or `filter` must be provided.",
        )
    if body.job_ids is not None and body.filter is not None:
        raise HTTPException(
            status_code=400,
            detail="Provide either `job_ids` or `filter`, not both.",
        )

    if body.job_ids is not None:
        # Legacy id-list branch — unchanged semantics.
        if len(body.job_ids) == 0:
            raise HTTPException(status_code=400, detail="`job_ids` cannot be empty.")
        # F69: cap even the id-list branch at BULK_FILTER_MAX. Nothing
        # in the old path prevented a client from POSTing 100k ids;
        # applying the same cap keeps the blast-radius story single-
        # bullet ("at most BULK_FILTER_MAX rows change per call").
        if len(body.job_ids) > BULK_FILTER_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"Too many job_ids ({len(body.job_ids)}). Max per request is {BULK_FILTER_MAX}.",
            )
        result = await db.execute(select(Job).where(Job.id.in_(body.job_ids)))
        jobs = result.scalars().all()
        metadata = {
            "mode": "ids",
            "job_ids": [str(j) for j in body.job_ids],
            "count": len(jobs),
        }
    else:
        # Filter branch — rebuild the same WHERE chain /jobs uses and
        # cap the matching set before mutating anything. The cap
        # check uses a COUNT so we don't pull thousands of rows into
        # memory just to reject them.
        assert body.filter is not None
        filter_query = await _build_bulk_filter_query(body.filter, db)
        count = (await db.execute(
            select(func.count()).select_from(filter_query.subquery())
        )).scalar() or 0
        if count == 0:
            raise HTTPException(status_code=400, detail="Filter matched zero jobs.")
        if count > BULK_FILTER_MAX:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Filter matches {count} jobs which exceeds the bulk cap of "
                    f"{BULK_FILTER_MAX}. Narrow the filter before retrying."
                ),
            )
        result = await db.execute(filter_query)
        jobs = result.scalars().all()
        # Serialize the filter into the audit log so post-hoc review
        # can reconstruct exactly which criteria triggered the mass
        # update. Using `model_dump(mode='json')` coerces UUID to str.
        metadata = {
            "mode": "filter",
            "filter": body.filter.model_dump(mode="json"),
            "count": len(jobs),
        }

    for j in jobs:
        j.status = body.action
    await db.commit()

    await log_action(
        db, user,
        action=f"bulk.{body.action}",
        resource="jobs",
        request=request,
        metadata=metadata,
    )

    return {"updated": len(jobs)}


# --- Feature A: manual job link submission -----------------------------------
#
# POST /jobs/submit-link — a sales user pastes an ATS URL; we detect the ATS
# from the hostname, fetch the single posting via that fetcher's single-job
# API, and run the result through the same `_upsert_job` pipeline the
# scanners use so classification, geography, and relevance scoring behave
# identically to scanned rows. Provenance is captured on two new columns:
# `Job.submission_source="manual_link"` and `Job.submitted_by_user_id`.
# Idempotency is free via the existing UNIQUE(external_id) — re-submitting
# the same URL returns the existing job with `is_new=false`.

import uuid as _uuid_mod  # noqa: E402 — Feature A additions
from pydantic import BaseModel as _BaseModel, ConfigDict as _ConfigDict, Field as _PField  # noqa: E402

# Per-user cap. Sales users paste 1-2 links at a time in practice; 20/hour
# is 10x headroom while still capping a runaway script / compromised session
# before it ingests thousands of rows. Enforced via the audit_logs table so
# we don't add a Redis dependency — audit writes already happen for every
# submit-link call.
_SUBMIT_LINK_RATE_LIMIT_PER_HOUR = 20


class SubmitJobLinkRequest(_BaseModel):
    """Payload for ``POST /jobs/submit-link``.

    ``extra="forbid"`` — a typo'd key produces a 422 at parse time rather
    than silently dropping the field and importing nothing.
    """
    model_config = _ConfigDict(extra="forbid")
    # Job.url is 1000 chars — match that cap here so the Pydantic reject
    # message is "URL too long" rather than a cryptic DB error later.
    url: str = _PField(..., min_length=10, max_length=1000)


class SubmitJobLinkResponse(_BaseModel):
    id: str
    title: str
    company_name: str
    platform: str
    is_new: bool
    status: str
    url: str


def _do_manual_ingest_sync(platform: str, slug: str, external_id: str, source_url: str, user_id) -> dict:
    """Run the manual-link upsert inside a sync SyncSession.

    Lives in its own function because the existing scan pipeline
    (`_upsert_job`, `_get_fetcher_for_platform`, `load_cluster_config_sync`)
    is all sync code — wrapping it in ``asyncio.to_thread`` from the async
    handler is cheaper than reimplementing the normalization/scoring chain
    for AsyncSession. Returns a plain outcome dict; does not raise
    HTTPException (that's the async caller's job based on the ``outcome``
    discriminator).

    Outcomes:
      * ``"created"``   — new Job row; provenance columns stamped.
      * ``"updated"``   — existing row refreshed via ``_upsert_job``'s
        update branch. Provenance is left alone — the original source wins.
      * ``"not_found"`` — ATS says the posting is gone. Nothing persists.
      * ``"no_fetcher"`` — parsed platform isn't in ``FETCHER_MAP``. The
        URL parser only emits supported platforms, so this is defensive.
    """
    from app.workers.tasks._db import SyncSession
    from app.workers.tasks._role_matching import load_cluster_config_sync
    from app.workers.tasks.scan_task import _upsert_job, _get_fetcher_for_platform

    session = SyncSession()
    try:
        # 1. Find or placeholder-create the Company. Sales admin renames
        #    / enriches later via /companies — we seed `name=slug` so
        #    the list view isn't blank, and `is_target=False` because a
        #    manual paste is not a curated target.
        company = session.execute(
            select(Company).where(Company.slug == slug)
        ).scalar_one_or_none()
        company_created = False
        if not company:
            company = Company(
                id=_uuid_mod.uuid4(),
                name=slug,
                slug=slug,
                is_target=False,
            )
            session.add(company)
            session.flush()
            company_created = True

        # 2. Find or placeholder-create the CompanyATSBoard. Manual
        #    submissions seed an INACTIVE board — manual paste is NOT
        #    opting the whole board into scanning. An admin can flip
        #    is_active later from the Platforms admin.
        board = session.execute(
            select(CompanyATSBoard).where(
                CompanyATSBoard.company_id == company.id,
                CompanyATSBoard.platform == platform,
                CompanyATSBoard.slug == slug,
            )
        ).scalar_one_or_none()
        if not board:
            board = CompanyATSBoard(
                id=_uuid_mod.uuid4(),
                company_id=company.id,
                platform=platform,
                slug=slug,
                is_active=False,
            )
            session.add(board)
            session.flush()

        # 3. Idempotency — if the scanner (or a prior paste) already
        #    imported this job, skip the HTTP call entirely.
        existing = session.execute(
            select(Job).where(Job.external_id == str(external_id))
        ).scalar_one_or_none()
        if existing:
            session.commit()
            return {
                "outcome": "updated",
                "id": str(existing.id),
                "title": existing.title,
                "company_name": company.name,
                "platform": existing.platform,
                "status": existing.status,
                "url": existing.url,
            }

        # 4. Fetch the single posting. `fetch_one` returns None on 404 /
        #    network error — treat as "job no longer listed".
        fetcher = _get_fetcher_for_platform(platform)
        if fetcher is None:
            session.rollback()
            return {"outcome": "no_fetcher"}

        raw_job = fetcher.fetch_one(slug, external_id)
        if not raw_job:
            if company_created:
                # Undo the placeholder Company/Board we created — a dead
                # posting shouldn't leave a shell company for ops cleanup.
                session.rollback()
            else:
                session.commit()
            return {"outcome": "not_found"}

        # 5. Normalize + score via the same pipeline the scanners use.
        #    Keep the user's pasted URL if the fetcher didn't populate one.
        if source_url and not raw_job.get("url"):
            raw_job["url"] = source_url

        cluster_config = load_cluster_config_sync(session)
        action = _upsert_job(session, company, board, raw_job, cluster_config)

        # 6. Re-load the job to stamp provenance. `_upsert_job` doesn't
        #    know about the two new columns; we set them here before
        #    commit. Only stamp on a brand-new row — an existing row's
        #    original `submission_source` must not be overwritten.
        job = session.execute(
            select(Job).where(Job.external_id == str(external_id))
        ).scalar_one()
        is_new = action == "new"
        if is_new:
            job.submission_source = "manual_link"
            job.submitted_by_user_id = user_id

        session.commit()
        return {
            "outcome": "created" if is_new else "updated",
            "id": str(job.id),
            "title": job.title,
            "company_name": company.name,
            "platform": job.platform,
            "status": job.status,
            "url": job.url,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/submit-link", response_model=SubmitJobLinkResponse)
async def submit_job_link(
    body: SubmitJobLinkRequest,
    request: Request,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    """Import a job from a pasted ATS URL.

    Supported hosts: Greenhouse, Lever, Ashby, Workable, BambooHR,
    SmartRecruiters, Jobvite, Recruitee. Non-ATS URLs (generic career
    pages, LinkedIn, Indeed) are rejected with a 400 — silent fallback
    to a generic scraper routinely produces garbage rows.

    The pasted link is parsed into ``(platform, slug, external_id)``,
    fetched via the matching fetcher's single-job API, normalized + scored
    through the same pipeline the scanners use, and persisted with
    ``submission_source="manual_link"`` + ``submitted_by_user_id`` for
    provenance. Re-submitting an already-imported URL is idempotent and
    returns ``is_new=false``.
    """
    from app.fetchers.url_parser import parse_job_url, UnsupportedJobUrlError

    # 1. Parse URL → (platform, slug, external_id). Any parse failure
    #    becomes a 400 with an actionable message, before DB work.
    try:
        parsed = parse_job_url(body.url)
    except UnsupportedJobUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 2. Rate limit via the audit log. Fail-open on a count query error
    #    (same posture the audit module itself takes for writes) — a
    #    transient audit-DB issue must not block legitimate submissions.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        recent_count = (await db.execute(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.user_id == user.id,
                AuditLog.action == "job.submit_link",
                AuditLog.created_at >= cutoff,
            )
        )).scalar() or 0
    except Exception:
        recent_count = 0
    if recent_count >= _SUBMIT_LINK_RATE_LIMIT_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit: at most {_SUBMIT_LINK_RATE_LIMIT_PER_HOUR} "
                "link submissions per hour."
            ),
        )

    # 3. Run the sync ingest pipeline in a worker thread so the event
    #    loop stays free during the ATS HTTP call + DB writes.
    import asyncio
    outcome = await asyncio.to_thread(
        _do_manual_ingest_sync,
        parsed.platform,
        parsed.slug,
        parsed.external_id,
        body.url,
        user.id,
    )

    if outcome.get("outcome") == "not_found":
        raise HTTPException(
            status_code=404,
            detail="Job posting is no longer listed on the ATS.",
        )
    if outcome.get("outcome") == "no_fetcher":
        raise HTTPException(
            status_code=500,
            detail=f"No fetcher registered for platform '{parsed.platform}'.",
        )

    is_new = outcome.get("outcome") == "created"

    # 4. Audit every call — including idempotent re-submissions — so ops
    #    can reconstruct who imported what and detect abuse. The same
    #    table feeds the rate-limit check above.
    await log_action(
        db, user,
        action="job.submit_link",
        resource="job",
        request=request,
        metadata={
            "url": body.url,
            "platform": parsed.platform,
            "slug": parsed.slug,
            "external_id": parsed.external_id,
            "is_new": is_new,
            "job_id": outcome.get("id"),
        },
    )

    return SubmitJobLinkResponse(
        id=outcome["id"],
        title=outcome["title"],
        company_name=outcome["company_name"],
        platform=outcome["platform"],
        status=outcome["status"],
        url=outcome["url"],
        is_new=is_new,
    )
