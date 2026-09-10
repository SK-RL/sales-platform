"""Server-side application submission.

Drives an ATS application form to completion with no human present, and
refuses to do so whenever it can't stand behind what it would send.

The gate, in order
------------------
Each check below routes to ``needs_user`` rather than failing, because
none of them are transient — retrying changes nothing until a person
acts. Only genuine mid-flight errors get ``failed`` (and a retry).

1. **Schema was extracted, not guessed.** ``extraction_mode="fallback"``
   means we invented a plausible form; a form we invented cannot be a
   form we filled correctly.
2. **We have an adapter for the platform.** See
   ``submitters.auto_submittable_platforms()``.
3. **Every required field resolved confidently.** ``blocking_gaps()``
   (F346) — no legal, EEO or compensation answer reached by inference.
4. **The site doesn't need a human.** CAPTCHA / login wall / emailed
   code, detected by the adapter before it fills anything.

Only then do we click submit, and only a positive confirmation from the
ATS flips the application to ``submitted``. "No exception was raised" is
not evidence of success — a form that fails inline validation raises
nothing, and recording that as submitted tells a user they applied to a
job they didn't.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession

logger = logging.getLogger(__name__)

# Statuses this task owns. `needs_user` and `in_flight` are new in F347
# and deliberately mirror the review-queue vocabulary the UI already
# uses elsewhere.
STATUS_IN_FLIGHT = "in_flight"
STATUS_NEEDS_USER = "needs_user"
STATUS_SUBMITTED = "submitted"
STATUS_FAILED = "failed"

MAX_RETRIES = 3


def _halt(session, app_row, reason: str, gaps: list[dict] | None = None) -> dict:
    """Park an application on ``needs_user`` with a reason the UI can show."""
    app_row.status = STATUS_NEEDS_USER
    app_row.platform_response = {
        "gate": "blocked",
        "reason": reason,
        "blocking": gaps or [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    session.commit()
    logger.info("apply_task: application %s needs user — %s", app_row.id, reason)
    return {"status": STATUS_NEEDS_USER, "reason": reason, "blocking": gaps or []}


@celery_app.task(bind=True, max_retries=MAX_RETRIES)
def submit_application_task(self, application_id: str, dry_run: bool = False) -> dict:
    """Fill and submit one application server-side.

    Returns a small dict for the caller / flower UI. All state lives on
    the ``Application`` row and its ``ApplicationSubmission``.
    """
    from app.models.answer_book import AnswerBookEntry
    from app.models.application import Application
    from app.models.application_submission import ApplicationSubmission
    from app.models.company import CompanyATSBoard
    from app.models.job import Job
    from app.models.resume import Resume
    from app.services.question_service import get_or_fetch_questions_sync
    from app.services.submitters import (
        SubmitField,
        auto_submittable_platforms,
        get_submitter,
    )
    from app.workers.tasks._answer_prep import (
        blocking_gaps,
        match_questions_to_answers,
    )

    session = SyncSession()
    try:
        app_row = session.execute(
            select(Application).where(Application.id == uuid.UUID(str(application_id)))
        ).scalar_one_or_none()
        if app_row is None:
            return {"status": "not_found", "application_id": str(application_id)}

        job = session.execute(
            select(Job).where(Job.id == app_row.job_id)
        ).scalar_one_or_none()
        if job is None:
            return _halt(session, app_row, "job no longer exists")

        # ── Gate 2: do we have an adapter? ─────────────────────────
        if job.platform not in auto_submittable_platforms():
            return _halt(
                session,
                app_row,
                f"no server-side submitter for platform '{job.platform}'",
            )

        board = session.execute(
            select(CompanyATSBoard).where(
                CompanyATSBoard.company_id == job.company_id,
                CompanyATSBoard.platform == job.platform,
                CompanyATSBoard.is_active.is_(True),
            )
        ).scalar_one_or_none()
        board_slug = board.slug if board else ""

        questions = get_or_fetch_questions_sync(session, job, board_slug)

        # ── Gate 1: extracted, not guessed ─────────────────────────
        if any(q.get("extraction_mode") == "fallback" for q in questions):
            return _halt(
                session,
                app_row,
                "we could not read this posting's real application form",
            )

        answers = session.execute(
            select(AnswerBookEntry).where(
                AnswerBookEntry.user_id == app_row.user_id,
                (AnswerBookEntry.resume_id.is_(None))
                | (AnswerBookEntry.resume_id == app_row.resume_id),
            )
        ).scalars().all()

        merged: dict[str, dict] = {}
        for entry in answers:
            # Resume-scoped entries override the shared base entry.
            if entry.question_key not in merged or entry.resume_id is not None:
                merged[entry.question_key] = {
                    "question_key": entry.question_key,
                    "answer": entry.answer or "",
                    "category": entry.category or "",
                    "source": entry.source or "base",
                }

        matched = match_questions_to_answers(questions, list(merged.values()))

        # ── Gate 3: every required field confidently resolved ──────
        # The resume is satisfied by the upload, not by an answer-book
        # entry, so it is declared here rather than left to look unmet.
        resume_row = session.execute(
            select(Resume).where(Resume.id == app_row.resume_id)
        ).scalar_one_or_none()
        satisfied = {"resume"} if getattr(resume_row, "file_data", None) else set()
        gaps = blocking_gaps(matched, satisfied_field_keys=satisfied)
        if gaps:
            return _halt(
                session,
                app_row,
                f"{len(gaps)} required field(s) need your answer",
                gaps,
            )

        resume = resume_row

        app_row.status = STATUS_IN_FLIGHT
        session.commit()

        fields = [
            SubmitField(
                field_key=m["field_key"],
                label=m["label"],
                field_type=m["field_type"],
                value=m["answer"],
                required=bool(m["required"]),
                options=m.get("options") or [],
                alternative_group=m.get("alternative_group") or "",
            )
            for m in matched
            # File fields are passed through even with no answer: the
            # adapter uploads the resume itself and needs to see the
            # field to know its alternative group is satisfied (F350).
            if m["field_type"] == "file" or m["answer"]
        ]

        submitter = get_submitter(job.platform)
        resume_path = _materialise_resume(resume)
        try:
            outcome = asyncio.run(
                submitter.submit(
                    job_url=job.url,
                    fields=fields,
                    resume_path=resume_path,
                    cover_letter_text=None,
                    dry_run=dry_run,
                )
            )
        finally:
            if resume_path:
                try:
                    os.unlink(resume_path)
                except OSError:
                    pass

        # ── Gate 4: the site wanted a human ────────────────────────
        if outcome.status == "blocked":
            return _halt(session, app_row, outcome.error or "site requires a human")

        if not outcome.ok:
            app_row.status = STATUS_FAILED
            app_row.platform_response = {
                "error": outcome.error,
                "detected_issues": outcome.detected_issues,
                "unplaceable_fields": outcome.unplaceable_fields,
                "attempt": self.request.retries + 1,
            }
            session.commit()
            logger.warning(
                "apply_task: submit failed for %s — %s", app_row.id, outcome.error
            )
            raise self.retry(
                exc=RuntimeError(outcome.error or "submit failed"),
                countdown=60 * (2 ** self.request.retries),
            )

        now = datetime.now(timezone.utc)
        session.add(
            ApplicationSubmission(
                id=uuid.uuid4(),
                application_id=app_row.id,
                submitted_at=now,
                job_url=job.url,
                ats_platform=job.platform,
                # Answers carry their provenance from the matcher so the
                # submission record shows not just what we sent but where
                # each value came from.
                answers_json=[
                    {
                        "question": m["label"] or m["field_key"],
                        "answer": m["answer"],
                        "source": m["match_source"],
                        "confidence": m["confidence"],
                    }
                    for m in matched
                ],
                payload_json={"field_count": len(fields), "dry_run": dry_run},
                cover_letter_text=None,
                screenshot_keys=outcome.screenshot_keys,
                confirmation_text=outcome.confirmation_text,
                detected_issues=outcome.detected_issues,
                profile_snapshot={
                    m["question_key"]: m["answer"]
                    for m in matched
                    if m.get("question_key")
                },
            )
        )
        # A dry run proves the form can be filled; it did not apply to
        # anything, so the application must not claim it did.
        if not dry_run:
            app_row.status = STATUS_SUBMITTED
            app_row.submitted_at = now
            app_row.apply_method = "api_submit"
            app_row.submission_source = "routine"
        session.commit()

        logger.info(
            "apply_task: %s application %s (%s)",
            "dry-ran" if dry_run else "submitted",
            app_row.id,
            job.platform,
        )
        return {
            "status": STATUS_SUBMITTED,
            "dry_run": dry_run,
            "confirmation": outcome.confirmation_text,
        }
    finally:
        session.close()


def _materialise_resume(resume) -> str | None:
    """Write the stored resume bytes to a temp file for upload.

    Resumes live in the DB as ``file_data`` bytes; Playwright's upload
    needs a path. Caller unlinks it.
    """
    if resume is None or not getattr(resume, "file_data", None):
        return None
    suffix = f".{(resume.file_type or 'pdf').lstrip('.')}"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="apply-resume-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(resume.file_data)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    return path


# Anything older than this that is still `in_flight` had its worker die
# mid-submit. Generous, because a Workday form with a slow upload can
# legitimately take minutes; the point is to catch dead rows, not slow
# ones.
STUCK_IN_FLIGHT_MINUTES = 30


@celery_app.task
def sweep_stuck_in_flight() -> dict:
    """Rescue applications abandoned mid-submit.

    ``in_flight`` is meant to be transient — the task always moves it on.
    But a worker OOM, a container restart or a lost DB connection between
    the status flip and the submitter's return leaves the row there
    forever, and the F347 endpoint refuses to re-submit anything already
    ``in_flight``. Without this sweeper that application is stuck with no
    way for the user to retry it.

    We move them to ``failed`` rather than retrying automatically: we do
    not know how far the previous attempt got. If it died *after* the
    submit click, the employer may already have the application, and
    silently re-sending would duplicate it. ``failed`` puts the decision
    in front of the user, who can re-submit or mark it applied by hand.
    """
    from datetime import timedelta

    from app.models.application import Application

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STUCK_IN_FLIGHT_MINUTES)
    session = SyncSession()
    try:
        stuck = session.execute(
            select(Application).where(
                Application.status == STATUS_IN_FLIGHT,
                Application.updated_at < cutoff,
            )
        ).scalars().all()

        for row in stuck:
            row.status = STATUS_FAILED
            row.platform_response = {
                "error": (
                    "submission was interrupted — the worker stopped before "
                    "the ATS confirmed. We don't know whether it went "
                    "through, so check the employer's site before retrying."
                ),
                "swept_at": datetime.now(timezone.utc).isoformat(),
                "stuck_for_minutes": STUCK_IN_FLIGHT_MINUTES,
            }
        if stuck:
            session.commit()
            logger.warning("apply_task: swept %d stuck in_flight application(s)", len(stuck))
        return {"swept": len(stuck)}
    finally:
        session.close()
