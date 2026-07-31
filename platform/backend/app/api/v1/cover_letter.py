"""Cover letter generation API."""

from app.ai_models import CLAUDE_OPUS
import asyncio
from typing import Literal
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

router = APIRouter(prefix="/cover-letter", tags=["cover-letter"])


# Regression finding 183: `tone` was a free-form str, so a caller could
# pass `tone="rude"` / `tone="aggressive_insulting"` and the string
# would end up verbatim in the Claude prompt. Lock it to the documented
# enum so invalid tones 422 at parse time instead of reaching the LLM.
CoverLetterTone = Literal["professional", "enthusiastic", "technical", "conversational"]


class CoverLetterRequest(BaseModel):
    # F157: reject unknown fields so a frontend typo (`ton=` instead of
    # `tone=`) doesn't silently fall back to the "professional" default
    # and mask the bug. Without `extra="forbid"`, Pydantic v2 defaults
    # to ignoring unknown keys — the caller thinks they set the field,
    # we think they didn't, and the generated letter is wrong.
    model_config = ConfigDict(extra="forbid")

    # F181: UUIDs must be typed as `UUID` (not `str`) so non-UUID input
    # 422s at parse time instead of bubbling a SQL cast error as 500.
    job_id: UUID
    resume_id: UUID | None = None
    tone: CoverLetterTone = "professional"


@router.post("/generate")
async def generate(body: CoverLetterRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Generate an AI-tailored cover letter for a specific job."""
    # Regression finding 183: raise HTTP 503 (Service Unavailable) when
    # the server isn't configured with an Anthropic key, not HTTP 500.
    # A missing config flag is not a server error — pages at 500 were
    # waking on-call every time a new environment was stood up without
    # the key. 503 is the semantically correct response and it doesn't
    # trip the error budget / alerting threshold for 5xx.
    if not get_settings().anthropic_api_key.get_secret_value():
        raise HTTPException(
            status_code=503,
            detail="AI cover letter generation is not configured on this server. Contact an administrator.",
        )

    # F236: per-user daily rate limit. Pre-fix, this endpoint had ZERO
    # limiting — a single user could generate thousands of letters per
    # day, blowing through the Anthropic budget. Now capped at 30/day
    # (default, configurable via `ai_cover_letter_daily_limit_per_user`).
    # 429 fires before the Claude call so the quota check is cheap;
    # the Retry-After header gives clients a useful "wait until midnight
    # UTC" value. Failed calls don't count (F170/F203 pattern).
    from app.utils.ai_rate_limit import check_ai_quota, log_ai_call
    from app.models.resume import AI_FEATURE_COVER_LETTER
    await check_ai_quota(db, user, AI_FEATURE_COVER_LETTER)

    # Load job + description
    job = (await db.execute(select(Job).where(Job.id == body.job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")

    company = (await db.execute(select(Company).where(Company.id == job.company_id))).scalar_one_or_none()
    company_name = company.name if company else "the company"

    desc = (await db.execute(select(JobDescription).where(JobDescription.job_id == job.id))).scalar_one_or_none()
    job_description = desc.text_content if desc else ""

    # Load resume
    resume_id = body.resume_id or user.active_resume_id
    if not resume_id:
        raise HTTPException(400, "No active resume. Upload a resume first.")

    resume = (await db.execute(select(Resume).where(Resume.id == resume_id, Resume.user_id == user.id))).scalar_one_or_none()
    if not resume:
        raise HTTPException(404, "Resume not found")
    if not resume.text_content:
        raise HTTPException(400, "Resume text not extracted yet. Wait for processing to complete.")

    # F157: `generate_cover_letter` is fully synchronous and issues a
    # blocking HTTP call to the Anthropic API (multi-second latency).
    # Calling it directly from an async handler pins the FastAPI event
    # loop for the duration of that round-trip, so a handful of
    # concurrent cover-letter requests were enough to stall every
    # other API endpoint on the same worker. `asyncio.to_thread` runs
    # the sync code on the default threadpool and leaves the loop
    # free to serve other requests.
    from app.workers.tasks._cover_letter import generate_cover_letter
    result = await asyncio.to_thread(
        generate_cover_letter,
        resume_text=resume.text_content,
        job_title=job.title,
        company_name=company_name,
        job_description=job_description,
        tone=body.tone,
    )

    # F236: log every call (success and failure) so the audit trail
    # is complete. `success=False` rows DON'T count toward the daily
    # quota — `count_ai_calls_today` filters them out — so an upstream
    # 502 doesn't burn the user's budget. Token counts pulled from the
    # `_cover_letter` worker's result block when present.
    is_success = not bool(result.get("error"))
    await log_ai_call(
        db, user, AI_FEATURE_COVER_LETTER,
        job_id=job.id,
        input_tokens=int(result.get("input_tokens", 0) or 0),
        output_tokens=int(result.get("output_tokens", 0) or 0),
        success=is_success,
    )

    # F238: training-data capture for cover_letter_quality. One row
    # per successful generation with (resume, JD, cover_letter, tone)
    # → outcome="generated". A future round will add UI events
    # ("kept" / "regenerated" / "applied") that update the label.
    # Side-effect-only: failure here doesn't break the user's call.
    if is_success:
        try:
            from app.utils.training_capture import capture_cover_letter_quality
            await capture_cover_letter_quality(
                db,
                user_id=user.id,
                resume_text=resume.text_content,
                job_title=job.title,
                job_description=job_description,
                cover_letter_text=result.get("cover_letter", ""),
                job_id=job.id,
                tone=body.tone,
                model_version=CLAUDE_OPUS,
            )
        except Exception:
            pass

    if result.get("error"):
        # Regression finding 183: upstream Claude API errors (rate
        # limit, upstream outage, safety refusal) map to 502 Bad
        # Gateway rather than 500, so dashboards can distinguish
        # "our bug" (500) from "upstream flaked" (502).
        raise HTTPException(502, result.get("customization_notes", "Upstream AI generation failed"))

    # F236: surface the post-call usage block so the frontend can update
    # its "X of Y left today" badge without a second round-trip to
    # /ai/usage. Same shape as the AI Tools panel reads.
    from app.utils.ai_rate_limit import usage_snapshot
    usage = await usage_snapshot(db, user)

    return {
        "cover_letter": result["cover_letter"],
        "key_points": result["key_points"],
        "customization_notes": result["customization_notes"],
        "tone": result["tone"],
        "job_title": job.title,
        "company_name": company_name,
        "usage": usage["features"][AI_FEATURE_COVER_LETTER],
    }
