"""Interview preparation AI API."""

from app.ai_models import CLAUDE_SONNET
import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.job import Job, JobDescription
from app.models.company import Company
from app.models.resume import Resume
from app.models.user import User
from app.api.deps import get_current_user

router = APIRouter(prefix="/interview-prep", tags=["interview-prep"])


class InterviewPrepRequest(BaseModel):
    # F157: reject unknown fields so a stray `resumeId` (camelCase) or
    # a copy-pasted `tone=` from the cover-letter client doesn't get
    # silently ignored. Pydantic v2's default is to accept-and-ignore,
    # which hides contract bugs on both sides.
    model_config = ConfigDict(extra="forbid")

    # F181: type as `UUID` (not `str`) so non-UUID input 422s at parse
    # time instead of bubbling a SQL cast error through as 500.
    job_id: UUID
    resume_id: UUID | None = None


@router.post("/generate")
async def generate(body: InterviewPrepRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Generate AI-powered interview preparation for a specific job."""
    # F183: missing Anthropic key is a config state, not a server
    # error — return 503 so on-call alerting doesn't trigger.
    if not get_settings().anthropic_api_key.get_secret_value():
        raise HTTPException(
            status_code=503,
            detail="AI interview prep is not configured on this server. Contact an administrator.",
        )

    # F236: per-user daily rate limit. Pre-fix this endpoint was
    # uncapped — a single user could spam it indefinitely. Now capped
    # at 10/day (default, configurable via
    # `ai_interview_prep_daily_limit_per_user`). 429 fires before the
    # Claude call; failed calls don't count (F170/F203).
    from app.utils.ai_rate_limit import check_ai_quota, log_ai_call, usage_snapshot
    from app.models.resume import AI_FEATURE_INTERVIEW_PREP
    await check_ai_quota(db, user, AI_FEATURE_INTERVIEW_PREP)

    job = (await db.execute(select(Job).where(Job.id == body.job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")

    company = (await db.execute(select(Company).where(Company.id == job.company_id))).scalar_one_or_none()
    company_name = company.name if company else "the company"
    company_info = ""
    if company:
        parts = []
        if company.industry:
            parts.append(f"Industry: {company.industry}")
        if company.employee_count:
            parts.append(f"Size: {company.employee_count} employees")
        if company.funding_stage:
            parts.append(f"Funding: {company.funding_stage}")
        if company.description:
            parts.append(f"About: {company.description[:300]}")
        company_info = ". ".join(parts)

    desc = (await db.execute(select(JobDescription).where(JobDescription.job_id == job.id))).scalar_one_or_none()
    job_description = desc.text_content if desc else ""

    resume_id = body.resume_id or user.active_resume_id
    if not resume_id:
        raise HTTPException(400, "No active resume. Upload a resume first.")

    resume = (await db.execute(select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id))).scalar_one_or_none()
    if not resume:
        raise HTTPException(404, "Resume not found")
    if not resume.text_content:
        raise HTTPException(400, "Resume text not extracted yet.")

    # F157: same event-loop blocking problem as cover-letter — the
    # Anthropic call underneath is synchronous and can take 5-30s, so
    # a handful of concurrent requests would otherwise starve every
    # other endpoint served by the same worker. Run it on the default
    # asyncio threadpool.
    from app.workers.tasks._interview_prep import generate_interview_prep
    result = await asyncio.to_thread(
        generate_interview_prep,
        resume_text=resume.text_content,
        job_title=job.title,
        company_name=company_name,
        job_description=job_description,
        company_info=company_info,
    )

    # F236: log every call (success and failure) so the audit trail is
    # complete. Failure rows don't count toward the quota.
    is_success = not bool(result.get("error"))
    await log_ai_call(
        db, user, AI_FEATURE_INTERVIEW_PREP,
        job_id=job.id,
        input_tokens=int(result.get("input_tokens", 0) or 0),
        output_tokens=int(result.get("output_tokens", 0) or 0),
        success=is_success,
    )

    # F238: training-data capture for interview_prep_quality. Same
    # pattern as cover-letter (one row per successful generation,
    # initial label "generated"). Side-effect-only.
    if is_success:
        try:
            from app.utils.training_capture import capture_interview_prep_quality
            await capture_interview_prep_quality(
                db,
                user_id=user.id,
                resume_text=resume.text_content,
                job_title=job.title,
                job_description=job_description,
                prep_payload={
                    "questions": result.get("questions", []),
                    "talking_points": result.get("talking_points", []),
                    "company_research": result.get("company_research", []),
                    "red_flags": result.get("red_flags", []),
                },
                job_id=job.id,
                model_version=CLAUDE_SONNET,
            )
        except Exception:
            pass

    if result.get("error"):
        # F183: upstream Anthropic API errors (rate limit, upstream
        # outage, safety refusal) map to 502 Bad Gateway, not 500.
        raise HTTPException(502, result.get("error_message", "Upstream AI generation failed"))

    # F236: usage block in the response so the frontend can update its
    # "X of Y left today" badge inline.
    usage = await usage_snapshot(db, user)
    return {
        "job_title": job.title,
        "company_name": company_name,
        "questions": result.get("questions", []),
        "talking_points": result.get("talking_points", []),
        "company_research": result.get("company_research", []),
        "red_flags": result.get("red_flags", []),
        "usage": usage["features"][AI_FEATURE_INTERVIEW_PREP],
    }
