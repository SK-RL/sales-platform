"""Potential Clients pipeline API with configurable stages."""

from uuid import UUID
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.application import Application
from app.models.pipeline import PotentialClient
from app.models.pipeline_stage import PipelineStage
from app.models.company import Company
from app.models.job import Job
from app.models.resume import Resume
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.schemas.pipeline import (
    PipelineItemOut,
    PipelineUpdate,
    ManualPipelineCardRequest,
    PIPELINE_MAX_PRIORITY,
    PIPELINE_MAX_NOTES_LENGTH,
)
from app.utils.audit import log_action

# Default stages seeded on first access
DEFAULT_STAGES = [
    {"key": "new_lead", "label": "New Lead", "color": "bg-blue-500", "sort_order": 0},
    {"key": "researching", "label": "Researching", "color": "bg-purple-500", "sort_order": 1},
    {"key": "qualified", "label": "Qualified", "color": "bg-emerald-500", "sort_order": 2},
    {"key": "outreach", "label": "Outreach", "color": "bg-amber-500", "sort_order": 3},
    {"key": "engaged", "label": "Engaged", "color": "bg-green-500", "sort_order": 4},
    {"key": "disqualified", "label": "Disqualified", "color": "bg-red-500", "sort_order": 5},
]


class PipelineCreateRequest(BaseModel):
    # F268 — extra="forbid". Tightening admin-side mutation schemas
    # so typos like ``compny_id`` 422 instead of silently dropping.
    model_config = ConfigDict(extra="forbid")

    # F181: body fields that carry a UUID should be typed as `UUID` so
    # Pydantic rejects "not-a-uuid" with HTTP 422 before the handler
    # runs. Previously `str` allowed garbage through, which surfaced
    # as 500 on the downstream `.where(Company.id == body.company_id)`
    # lookup when SQLAlchemy couldn't cast the text to a uuid column.
    company_id: UUID
    stage: str = "new_lead"
    priority: int = Field(default=0, ge=0, le=PIPELINE_MAX_PRIORITY)
    notes: str = Field(default="", max_length=PIPELINE_MAX_NOTES_LENGTH)


_STAGE_KEY_MAX = 50
_STAGE_LABEL_MAX = 100
_STAGE_COLOR_MAX = 60


class StageCreate(BaseModel):
    # F268 — strict on stage CRUD too.
    model_config = ConfigDict(extra="forbid")

    # F288 (closes F149 b) — cap + HTML-strip on key/label so an
    # admin payload like ``label="<img src=x onerror=alert(1)>"``
    # can't be persisted and rendered verbatim in the Kanban
    # column header. ``key`` is also restricted to a slug-safe
    # pattern so it can't carry markup or whitespace; ``label`` is
    # allowed mixed-case + spaces but stripped of any tags. Same
    # ``strip_html_tags`` defense feedback (F162), role-cluster
    # display_name (F285), and platforms board company_name (F287)
    # use.
    key: str = Field(..., min_length=1, max_length=_STAGE_KEY_MAX, pattern=r"^[a-z0-9_\-]+$")
    label: str = Field(..., min_length=1, max_length=_STAGE_LABEL_MAX)
    color: str = Field(default="bg-gray-500", max_length=_STAGE_COLOR_MAX)
    sort_order: int = 0

    @field_validator("label")
    @classmethod
    def _strip_html_label(cls, v: str) -> str:
        from app.utils.sanitize import strip_html_tags
        return strip_html_tags(v).strip()


class StageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=_STAGE_LABEL_MAX)
    color: str | None = Field(default=None, max_length=_STAGE_COLOR_MAX)
    sort_order: int | None = None

    @field_validator("label")
    @classmethod
    def _strip_html_label_update(cls, v):
        # F288: same defense as StageCreate. PATCH path was a
        # parallel XSS vector — pre-fix you could create a clean
        # stage and then PATCH the label to a payload, bypassing
        # the create-side guard.
        if v is None:
            return v
        from app.utils.sanitize import strip_html_tags
        return strip_html_tags(v).strip()


router = APIRouter(prefix="/pipeline", tags=["pipeline"])


async def _get_active_stages(db: AsyncSession) -> list[PipelineStage]:
    """Get active pipeline stages, auto-seeding defaults if table is empty."""
    result = await db.execute(
        select(PipelineStage).where(PipelineStage.is_active == True).order_by(PipelineStage.sort_order)
    )
    stages = list(result.scalars().all())
    if not stages:
        # Seed defaults
        for s in DEFAULT_STAGES:
            stage = PipelineStage(**s)
            db.add(stage)
        await db.commit()
        result = await db.execute(
            select(PipelineStage).where(PipelineStage.is_active == True).order_by(PipelineStage.sort_order)
        )
        stages = list(result.scalars().all())
    return stages


async def _get_stage_keys(db: AsyncSession) -> list[str]:
    stages = await _get_active_stages(db)
    return [s.key for s in stages]


# ── Stage management (admin/super_admin) ──

@router.get("/stages")
async def list_stages(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all pipeline stages (including inactive for admin)."""
    if user.role in ("admin", "super_admin"):
        result = await db.execute(select(PipelineStage).order_by(PipelineStage.sort_order))
    else:
        result = await db.execute(
            select(PipelineStage).where(PipelineStage.is_active == True).order_by(PipelineStage.sort_order)
        )
    stages = result.scalars().all()
    if not stages:
        stages = await _get_active_stages(db)
    return {
        "items": [
            {
                "id": str(s.id),
                "key": s.key,
                "label": s.label,
                "color": s.color,
                "sort_order": s.sort_order,
                "is_active": s.is_active,
            }
            for s in stages
        ]
    }


@router.post("/stages", status_code=201)
async def create_stage(
    body: StageCreate,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Add a new pipeline stage (admin/super_admin)."""
    # Check key uniqueness
    existing = (await db.execute(
        select(PipelineStage).where(PipelineStage.key == body.key)
    )).scalar_one_or_none()
    if existing:
        if not existing.is_active:
            # Reactivate
            existing.is_active = True
            existing.label = body.label
            existing.color = body.color
            existing.sort_order = body.sort_order
            await db.commit()
            return {"ok": True, "id": str(existing.id), "reactivated": True}
        raise HTTPException(400, "Stage key already exists")

    stage = PipelineStage(key=body.key, label=body.label, color=body.color, sort_order=body.sort_order)
    db.add(stage)
    # F327 (race-safe): the lookup-then-insert above is TOCTOU-
    # vulnerable. Two concurrent admin POSTs with the same stage
    # ``key`` both pass the lookup, both INSERT, and the second
    # blows up with an unhandled IntegrityError on the
    # ``pipeline_stages.key`` UNIQUE that escapes as a bare HTTP
    # 500. PipelineStage.key is declared as ``mapped_column(
    # String(50), unique=True, ...)`` — no explicit constraint
    # name, so Postgres auto-generates ``pipeline_stages_key_key``
    # per the ``<table>_<column>_key`` convention. We match on
    # the substring ``pipeline_stages_key`` so a future migration
    # that renames the constraint to ``uq_pipeline_stages_key`` or
    # similar still triggers the translation. Other IntegrityErrors
    # propagate as 500 so genuinely-different bugs aren't hidden.
    #
    # Same shape as F325 (profiles), F326 (auth register), F324
    # (work-time pending), F323 (routine-targets), F322 (answer-
    # book), F321 (applications), F320 (potential-clients), F316
    # (jobs), F282 (contacts), F281 (reviews).
    #
    # Note: the existing-row reactivation path (lines above)
    # does NOT need the same wrap because it's an UPDATE on a
    # row already pinned by primary key; the only race window is
    # in the INSERT branch.
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        diag = getattr(getattr(exc, "orig", None), "diag", None)
        constraint_name = (
            getattr(diag, "constraint_name", "") if diag else ""
        ) or ""
        err_text = str(exc).lower()
        if (
            "pipeline_stages_key" in constraint_name
            or "pipeline_stages_key" in err_text
        ):
            # Re-raise as the same 400 the lookup-check produces so
            # the admin UI shows a single consistent error regardless
            # of timing.
            raise HTTPException(400, "Stage key already exists")
        raise
    await db.refresh(stage)

    await log_action(
        db, user, action="pipeline.stage_create", resource="pipeline_stage",
        request=request, metadata={"stage_id": str(stage.id), "key": body.key},
    )

    return {"ok": True, "id": str(stage.id)}


@router.patch("/stages/{stage_id}")
async def update_stage(
    stage_id: UUID,
    body: StageUpdate,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a stage label, color, or sort order (admin/super_admin)."""
    stage = await db.get(PipelineStage, stage_id)
    if not stage:
        raise HTTPException(404, "Stage not found")

    if body.label is not None:
        stage.label = body.label
    if body.color is not None:
        stage.color = body.color
    if body.sort_order is not None:
        stage.sort_order = body.sort_order

    await db.commit()
    return {"ok": True, "id": str(stage.id), "label": stage.label, "color": stage.color}


@router.delete("/stages/{stage_id}")
async def deactivate_stage(
    stage_id: UUID,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a pipeline stage (soft-delete, data preserved)."""
    stage = await db.get(PipelineStage, stage_id)
    if not stage:
        raise HTTPException(404, "Stage not found")

    # Check if any pipeline items use this stage
    count = (await db.execute(
        select(func.count()).select_from(PotentialClient).where(PotentialClient.stage == stage.key)
    )).scalar() or 0

    stage.is_active = False
    await db.commit()
    return {
        "ok": True,
        "message": f"Stage '{stage.label}' deactivated (data preserved)",
        "items_in_stage": count,
    }


# ── Pipeline CRUD ──

@router.get("")
async def get_pipeline(
    stage: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    active_stages = await _get_active_stages(db)
    stage_keys = [s.key for s in active_stages]

    # Regression finding 215(b): `stage` was typed `str | None` with no
    # check, so `?stage=wat`, `?stage=<script>`, `?stage=INVALID` all
    # returned HTTP 200 with total=0 and six empty stage groups — same
    # silent-drop class as F187/F191/F218. A Literal here isn't usable
    # because stages are user-configurable in the DB (see
    # `/pipeline/stages` CRUD above); run the check at runtime against
    # the active stage list we just loaded. `relevant`-style meta-values
    # aren't defined for pipeline, so only the exact keys are allowed.
    if stage is not None and stage not in stage_keys:
        raise HTTPException(
            status_code=422,
            detail=(
                "Invalid stage. Must be one of: "
                + ", ".join(sorted(stage_keys))
                + " (configure via /pipeline/stages)."
            ),
        )

    query = select(PotentialClient).options(
        joinedload(PotentialClient.company),
        joinedload(PotentialClient.applicant),
        joinedload(PotentialClient.resume),
    )
    if stage:
        query = query.where(PotentialClient.stage == stage)
    query = query.order_by(PotentialClient.priority.desc(), PotentialClient.created_at.desc())

    result = await db.execute(query)
    clients = result.unique().scalars().all()

    # Gather company IDs to compute live metrics
    company_ids = [c.company_id for c in clients if c.company_id]

    open_roles_map: dict[str, int] = {}
    accepted_map: dict[str, int] = {}
    velocity_map: dict[str, str] = {}
    last_job_map: dict[str, datetime | None] = {}
    # F266 — applications-per-card map. Pre-fix the kanban gave no
    # signal that 19/23 cards were empty under drill-down; surfacing
    # the count up-front (as a small badge on the card) lets admins
    # see at a glance which targets actually have apply activity.
    apps_map: dict[str, int] = {}

    if company_ids:
        open_result = await db.execute(
            select(Job.company_id, func.count(Job.id))
            .where(Job.company_id.in_(company_ids), Job.status.in_(["new", "accepted", "under_review"]))
            .group_by(Job.company_id)
        )
        for cid, cnt in open_result:
            open_roles_map[str(cid)] = cnt

        acc_result = await db.execute(
            select(Job.company_id, func.count(Job.id))
            .where(Job.company_id.in_(company_ids), Job.status == "accepted")
            .group_by(Job.company_id)
        )
        for cid, cnt in acc_result:
            accepted_map[str(cid)] = cnt

        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        vel_result = await db.execute(
            select(Job.company_id, func.count(Job.id))
            .where(Job.company_id.in_(company_ids), Job.first_seen_at >= thirty_days_ago)
            .group_by(Job.company_id)
        )
        for cid, cnt in vel_result:
            cid_str = str(cid)
            velocity_map[cid_str] = "high" if cnt > 5 else "medium" if cnt >= 2 else "low"

        last_result = await db.execute(
            select(Job.company_id, func.max(Job.first_seen_at))
            .where(Job.company_id.in_(company_ids))
            .group_by(Job.company_id)
        )
        for cid, last_seen in last_result:
            last_job_map[str(cid)] = last_seen

        # F266 — count submitted applications per company. Mirrors the
        # ``accepted_map`` shape so the frontend can render a small
        # "N apps" badge on each card. The ``Application.company_id``
        # column is denormalised (F261) so this is a single-table
        # COUNT — no Application⨝Job join cost.
        apps_result = await db.execute(
            select(Application.company_id, func.count(Application.id))
            .where(Application.company_id.in_(company_ids))
            .group_by(Application.company_id)
        )
        for cid, cnt in apps_result:
            apps_map[str(cid)] = cnt

    items = []
    for c in clients:
        item = PipelineItemOut.model_validate(c)
        item.company_name = c.company.name if c.company else None
        item.company_website = c.company.website if c.company else None
        item.applied_by_name = c.applicant.name if c.applicant else None
        item.resume_label = c.resume.label if c.resume else None
        items.append(item)

    items_data = []
    for item, c in zip(items, clients):
        d = item.model_dump(mode="json")
        cid = str(c.company_id) if c.company_id else ""
        d["total_open_roles"] = open_roles_map.get(cid, 0)
        # Regression finding 192: `accepted_jobs_count` now ALWAYS
        # reflects the live count of `Job WHERE status='accepted'` for
        # this company, never the stored `PotentialClient.accepted_jobs_count`
        # column (which was monotonically incremented on every accept
        # review event and never decremented on reject/flip, so it drifted
        # from reality). Dropping the `.get("accepted_jobs_count", 0)`
        # fallback means a company with zero accepted jobs shows 0 here
        # consistently — matching what the detail endpoint now returns.
        d["accepted_jobs_count"] = accepted_map.get(cid, 0)
        # F266: applications_count is the count of submitted apps under
        # this card's company — drives the new "N apps" badge on each
        # PipelineCard. Falls through to 0 for cards without a
        # company_id (orphan rows; rare).
        d["applications_count"] = apps_map.get(cid, 0)
        d["hiring_velocity"] = velocity_map.get(cid, "low")
        d["last_job_at"] = last_job_map.get(cid, None)
        if d["last_job_at"]:
            d["last_job_at"] = d["last_job_at"].isoformat()
        items_data.append(d)

    # Group by stage (include all active stages even if empty)
    grouped = {s: [] for s in stage_keys}
    for item in items_data:
        stage_key = item["stage"]
        if stage_key in grouped:
            grouped[stage_key].append(item)
        else:
            # Item in a deactivated stage — still show it
            grouped.setdefault(stage_key, []).append(item)

    # Return stage config along with items
    stages_config = [
        {"key": s.key, "label": s.label, "color": s.color}
        for s in active_stages
    ]

    # Regression finding 215(a): previously `items` was a `dict[str,
    # list[...]]` keyed by stage — a shape drift from the canonical
    # `{items, total, page, page_size, total_pages}` pagination envelope
    # documented in CLAUDE.md. A generic `<PaginatedList>` component
    # that expected `items: Array<T>` crashed on `.map()` / silently
    # mis-interpreted `.length === 6` (the 6 stage keys) as "6 rows".
    #
    # Fix: `items` is now the flat list (canonical shape), `by_stage`
    # carries the kanban-grouped view for the PipelinePage UI. No
    # pagination fields yet — the pipeline is expected to stay small
    # relative to /jobs, and per-row live-metric subqueries wouldn't
    # benefit from LIMIT/OFFSET at this volume; if it grows past ~1k
    # rows, add `page`/`page_size` with the same 4-subquery pattern
    # bounded to just the slice.
    return {
        "stages": stage_keys,
        "stages_config": stages_config,
        "items": items_data,          # flat canonical list (F215 fix)
        "by_stage": grouped,          # kanban-grouped view
        "total": len(items_data),
    }


@router.post("", status_code=201)
async def create_pipeline_entry(
    body: PipelineCreateRequest,
    request: Request,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    """Manually add a company to the pipeline."""
    company = (await db.execute(select(Company).where(Company.id == body.company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    existing = (await db.execute(
        select(PotentialClient).where(PotentialClient.company_id == body.company_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Company is already in the pipeline")

    stage_keys = await _get_stage_keys(db)
    if body.stage not in stage_keys:
        raise HTTPException(status_code=400, detail=f"Invalid stage. Must be one of: {', '.join(stage_keys)}")

    client = PotentialClient(
        company_id=body.company_id,
        stage=body.stage,
        priority=body.priority,
        notes=body.notes,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)

    try:
        from app.workers.tasks.enrichment_task import enrich_company
        enrich_company.delay(str(company.id))
    except Exception:
        pass

    await log_action(
        db, user,
        action="pipeline.create",
        resource="pipeline",
        request=request,
        metadata={"client_id": str(client.id), "company_id": str(body.company_id), "stage": body.stage},
    )

    return {"ok": True, "id": str(client.id), "company_name": company.name}


def _slugify_company(name: str) -> str:
    """Conservative slugifier for manual-card company creation.

    Same shape as ``AdzunaFetcher._slugify`` — lowercase, punctuation
    to dashes, bounded — so the same employer entered manually and
    later discovered by a scan lands on ONE Company row instead of
    a near-duplicate pair.
    """
    import re

    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:200] or "manual-company"


@router.post("/manual", status_code=201)
async def create_manual_pipeline_card(
    body: ManualPipelineCardRequest,
    request: Request,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    """Ticket bac45b42 — create a pipeline card from free-form fields.

    Unlike ``POST /pipeline`` (which requires an existing
    ``company_id``), this endpoint find-or-creates the Company from
    the typed name + website, so sales can card up a prospect that
    the scraper has never seen. The four mandatory fields (company
    name, website, JD link, applied identity) are enforced by the
    request schema; everything else lands in the ``manual_card``
    JSONB on the PotentialClient row.
    """
    slug = _slugify_company(body.company_name)
    company = (await db.execute(
        select(Company).where(Company.slug == slug)
    )).scalar_one_or_none()
    if not company:
        company = Company(
            name=body.company_name.strip(),
            slug=slug,
            website=body.company_website.strip(),
            linkedin_url=(body.linkedin_url or "").strip(),
            funding_stage=(body.funding_status or "").strip(),
            is_target=False,
        )
        db.add(company)
        await db.flush()
    else:
        # Company already known — backfill website/linkedin if the
        # scraped record lacks them. Presence wins: never overwrite
        # non-empty scraped data with hand-typed values.
        if not company.website and body.company_website:
            company.website = body.company_website.strip()
        if not company.linkedin_url and body.linkedin_url:
            company.linkedin_url = body.linkedin_url.strip()

    existing = (await db.execute(
        select(PotentialClient).where(PotentialClient.company_id == company.id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"'{company.name}' is already in the pipeline",
        )

    stage_keys = await _get_stage_keys(db)
    stage = body.stage or (stage_keys[0] if stage_keys else "new_lead")
    if stage not in stage_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage. Must be one of: {', '.join(stage_keys)}",
        )

    # Everything card-specific goes into the JSONB — drop Nones so
    # the stored dict only carries what the user actually filled in.
    card_fields = {
        k: v.strip()
        for k, v in {
            "jd_link": body.jd_link,
            "applied_id": body.applied_id,
            "linkedin_url": body.linkedin_url,
            "funding_status": body.funding_status,
            "designation": body.designation,
            "salary_current": body.salary_current,
            "salary_expected": body.salary_expected,
            "interviewer_name": body.interviewer_name,
            "interviewer_email": body.interviewer_email,
            "interviewee_name": body.interviewee_name,
            "interviewee_type": body.interviewee_type,
            "jd_description": body.jd_description,
            "details": body.details,
        }.items()
        if v is not None and v.strip()
    }

    client = PotentialClient(
        company_id=company.id,
        stage=stage,
        priority=body.priority,
        notes=body.notes,
        manual_card=card_fields,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)

    await log_action(
        db, user,
        action="pipeline.create_manual",
        resource="pipeline",
        request=request,
        metadata={
            "client_id": str(client.id),
            "company_id": str(company.id),
            "company_name": company.name,
            "stage": stage,
        },
    )

    return {"ok": True, "id": str(client.id), "company_name": company.name}


@router.get("/{client_id}", response_model=PipelineItemOut)
async def get_client(client_id: UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PotentialClient).options(joinedload(PotentialClient.company)).where(PotentialClient.id == client_id)
    )
    client = result.unique().scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    item = PipelineItemOut.model_validate(client)
    item.company_name = client.company.name if client.company else None
    item.company_website = client.company.website if client.company else None

    # Regression finding 192: the stored `accepted_jobs_count` column
    # is an increment-only counter — `reviews.py` bumped it by 1 on
    # every `accept` event but never decremented it on `reject`, so a
    # job that flipped accept→reject→accept was counted twice. The
    # listing endpoint (get_pipeline above) has always overridden with
    # a live COUNT(*) of `Job WHERE status='accepted'` for the
    # company; detail was returning the raw (drifted) column. On a
    # live Supabase row with 4 review flip-flops on the same job,
    # listing returned 1 and detail returned 2 — the kanban and the
    # detail panel disagreed. We now do the same live count here so
    # the two endpoints agree.
    if client.company_id:
        live_accepted = (await db.execute(
            select(func.count(Job.id)).where(
                Job.company_id == client.company_id,
                Job.status == "accepted",
            )
        )).scalar() or 0
        item.accepted_jobs_count = int(live_accepted)
        # F266 — also surface the live applications count on the
        # detail endpoint so the frontend's card badge stays
        # consistent whether rendered from /pipeline (list) or
        # /pipeline/{id} (detail).
        live_apps = (await db.execute(
            select(func.count(Application.id)).where(
                Application.company_id == client.company_id
            )
        )).scalar() or 0
        item.applications_count = int(live_apps)
    else:
        item.accepted_jobs_count = 0
        item.applications_count = 0

    return item


@router.get("/{client_id}/applications")
async def list_client_applications(
    client_id: UUID,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Drill-down: every application under this pipeline card.

    F261 — Team Pipeline Tracker. The kanban view shows one card per
    company; clicking a card opens a side panel that needs to list
    every application the team has submitted under that company so
    the operator can see "we have 3 active applications here, two
    are in Interview 1, one is still Applied".

    Admin-gated to match the team-pipeline RBAC. The per-user
    Applications page (GET /applications) covers the non-admin case.

    Returns the same row shape as GET /applications/team so the
    frontend can reuse one row component.
    """
    # Resolve the pipeline entry so we have the company_id. We don't
    # accept ``company_id`` directly in the URL because the side
    # panel sits under a pipeline card, and the operator's mental
    # model is "applications under THIS card" not "applications
    # under company X" (companies can have multiple cards over time
    # if the pipeline entry is hard-deleted and recreated).
    client = (await db.execute(
        select(PotentialClient).where(PotentialClient.id == client_id)
    )).scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    rows = (await db.execute(
        select(
            Application,
            Job,
            Resume.label.label("resume_label"),
            Resume.filename.label("resume_filename"),
            User.name.label("applicant_name"),
            User.email.label("applicant_email"),
        )
        .join(Job, Application.job_id == Job.id)
        .join(User, Application.user_id == User.id)
        .join(Resume, Application.resume_id == Resume.id)
        .where(Application.company_id == client.company_id)
        .order_by(
            func.coalesce(Application.applied_at, Application.created_at).desc()
        )
    )).all()

    items = []
    for app, job, resume_label, resume_filename, applicant_name, applicant_email in rows:
        items.append({
            "id": str(app.id),
            "job_id": str(app.job_id),
            "job_title": job.title,
            "job_url": job.url,
            "platform": job.platform,
            "user_id": str(app.user_id),
            "applicant_name": applicant_name,
            "applicant_email": applicant_email,
            "resume_id": str(app.resume_id),
            "resume_label": resume_label or resume_filename or "",
            "status": app.status,
            "stage_key": app.stage_key,
            "submission_source": app.submission_source,
            "applied_at": app.applied_at.isoformat() if app.applied_at else None,
            "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
            "created_at": app.created_at.isoformat(),
            "notes": app.notes,
        })
    return {"items": items, "total": len(items)}


@router.patch("/{client_id}", response_model=PipelineItemOut)
async def update_client(
    client_id: UUID, body: PipelineUpdate,
    request: Request,
    user: User = Depends(require_role("admin", "reviewer")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PotentialClient).options(joinedload(PotentialClient.company)).where(PotentialClient.id == client_id)
    )
    client = result.unique().scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    if body.stage is not None:
        # Validate against known stage keys — same check as POST — so cards
        # can't be dropped into a non-existent / deactivated stage via the
        # API (and then vanish from the kanban board).
        stage_keys = await _get_stage_keys(db)
        if body.stage not in stage_keys:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid stage. Must be one of: {', '.join(stage_keys)}",
            )
        client.stage = body.stage
    if body.priority is not None:
        client.priority = body.priority
    # Regression finding 111: validate FK references before commit so a
    # non-existent UUID returns 404 instead of a raw 500 IntegrityError.
    if body.assigned_to is not None:
        if (await db.get(User, body.assigned_to)) is None:
            raise HTTPException(status_code=404, detail="assigned_to user not found")
        client.assigned_to = body.assigned_to
    if body.resume_id is not None:
        if (await db.get(Resume, body.resume_id)) is None:
            raise HTTPException(status_code=404, detail="resume_id not found")
        client.resume_id = body.resume_id
    if body.applied_by is not None:
        if (await db.get(User, body.applied_by)) is None:
            raise HTTPException(status_code=404, detail="applied_by user not found")
        client.applied_by = body.applied_by
    if body.notes is not None:
        client.notes = body.notes

    await db.commit()
    await db.refresh(client)

    await log_action(
        db, user,
        action="pipeline.update",
        resource="pipeline",
        request=request,
        metadata={"client_id": str(client_id), "fields": list(body.model_dump(exclude_unset=True).keys())},
    )

    item = PipelineItemOut.model_validate(client)
    item.company_name = client.company.name if client.company else None
    item.company_website = client.company.website if client.company else None
    return item


@router.delete("/{client_id}", status_code=204)
async def delete_client(
    client_id: UUID,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a pipeline entry (admin-only).

    Regression finding 173: previously the router only exposed PATCH for
    stage changes, so accidentally-created entries and regression/test
    probes accumulated indefinitely in the `disqualified` stage. The
    soft-delete-via-stage workaround preserved sales history for audit
    but provided no path to remove genuinely junk rows.

    This endpoint is locked to admin (not admin+reviewer) so the normal
    pipeline workflow still goes through PATCH — only an admin cleaning
    up test data or mis-created leads can physically remove a row.
    The action is logged via `log_action` so the deletion itself is
    still traceable in the audit trail after the row is gone.
    """
    client = await db.get(PotentialClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Capture identity fields for the audit log BEFORE the row is gone.
    snapshot = {
        "client_id": str(client.id),
        "company_id": str(client.company_id) if client.company_id else None,
        "stage": client.stage,
    }

    await db.delete(client)
    await db.commit()

    await log_action(
        db, user,
        action="pipeline.delete",
        resource="pipeline",
        request=request,
        metadata=snapshot,
    )
    return None
