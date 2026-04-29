"""F311 — /resume/{id}/scores filters items to active job statuses.

Companion to F310. F310 stops the rescore TASK from scoring
expired/archived jobs. F311 stops the user-facing READ endpoint
from surfacing pre-existing stale ResumeScore rows for jobs that
were active when scored and have since flipped to expired/archived.

Without this read-side filter, a user who reviewed jobs months ago
and triggered a rescore today would see scores against jobs that
no longer exist on the source ATS — confusing UX, the user can't
actually apply to the job. F311 mirrors the active-statuses set
maintenance_task and F310 use.

The summary stats (``total_all``, ``avg_score_all``) deliberately
stay lifetime-totals so the user can see "you've scored 5000 jobs
total, 3000 currently active" rather than just the active count.
"""
from __future__ import annotations

import os
import pathlib

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f311")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "resume.py").read_text()


def test_scores_handler_filters_active_status():
    """``base_query`` must include ``Job.status.in_(...)`` filter
    so expired/archived rows don't surface in the items list.
    """
    src = _read()
    handler_start = src.find("async def get_resume_scores")
    handler_end = src.find("@router.", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    body = src[handler_start:handler_end]
    assert "Job.status.in_(" in body, (
        "F311 regression: get_resume_scores no longer filters on "
        "Job.status. Expired and archived rows will surface in "
        "the user's score list again."
    )


def test_active_status_set_matches_f310():
    """Same active-statuses set as F310 (resume rescore task) so
    rescore-time and read-time can't disagree on which jobs are
    'active'.
    """
    src = _read()
    handler_start = src.find("async def get_resume_scores")
    handler_end = src.find("@router.", handler_start + 1)
    body = src[handler_start:handler_end] if handler_end > 0 else src[handler_start:]
    assert (
        '"new"' in body and '"under_review"' in body and '"accepted"' in body
    ), (
        "F311 regression: active-statuses set drifted from F310. "
        "Rescore-time and read-time will show different rows."
    )
