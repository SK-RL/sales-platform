"""F406 — routes for the project-idea library and the stage-1 evaluation.

Own prefix on purpose: under ``/applications`` the literal paths were
shadowed by ``GET /applications/{app_id}`` (a UUID parser) and answered
422. Per-application routes stay in ``applications.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone  # noqa: F401
from uuid import UUID  # noqa: F401

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.applications import _is_uuid, _proof_redis  # noqa: F401
from app.database import get_db
from app.models.application import Application
from app.models.job import Job
from app.models.user import User

router = APIRouter(prefix="/project-ideas", tags=["project-ideas"])


@router.get("/library")
async def project_ideas_library(user: User = Depends(get_current_user)):
    """Ideas that fit most of the openings we track, with the share of
    job descriptions each covers (computed from our corpus)."""
    import json

    from app.workers.tasks.proof_task import GENERIC_KEY

    try:
        raw = _proof_redis().get(GENERIC_KEY.format(user_id=user.id))
    except Exception:
        raw = None
    return json.loads(raw) if raw else {"ideas": [], "running": False}


@router.post("/library/draft")
async def draft_project_ideas_library(user: User = Depends(get_current_user)):
    import json

    from app.workers.tasks.proof_task import GENERIC_KEY, generic_project_ideas_task

    task = generic_project_ideas_task.apply_async(args=[str(user.id)], retry=False)
    try:
        r = _proof_redis()
        prev = json.loads(r.get(GENERIC_KEY.format(user_id=user.id)) or "{}")
        r.set(GENERIC_KEY.format(user_id=user.id), json.dumps({**prev, "running": True, "task_id": task.id}), ex=14 * 24 * 3600)
    except Exception:
        pass
    return {"queued": True, "task_id": task.id}


class ProjectIdeasEvalRequest(BaseModel):
    application_ids: list[str] | None = None
    limit: int = 10


@router.post("/eval")
async def run_project_ideas_eval(
    body: ProjectIdeasEvalRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stage-1 test: run the idea pipeline over the user's applications
    (newest first, distinct companies) and keep a report."""
    from app.workers.tasks.proof_task import project_ideas_eval_task

    ids = [i for i in (body.application_ids or []) if _is_uuid(i)]
    if not ids:
        rows = (await db.execute(
            select(Application.id, Job.company_id).join(Job, Job.id == Application.job_id)
            .where(Application.user_id == user.id).order_by(Application.created_at.desc()).limit(200)
        )).all()
        seen: set = set()
        for app_id, company_id in rows:
            if company_id in seen:
                continue
            seen.add(company_id)
            ids.append(str(app_id))
            if len(ids) >= max(1, min(body.limit, 40)):
                break
    if not ids:
        raise HTTPException(status_code=400, detail="No applications to evaluate")
    task = project_ideas_eval_task.apply_async(args=[str(user.id), ids], retry=False)
    return {"queued": True, "task_id": task.id, "applications": ids}


@router.get("/eval")
async def get_project_ideas_eval(user: User = Depends(get_current_user)):
    import json

    from app.workers.tasks.proof_task import EVAL_KEY

    try:
        raw = _proof_redis().get(EVAL_KEY.format(user_id=user.id))
    except Exception:
        raw = None
    return json.loads(raw) if raw else {"rows": [], "running": False}


