"""F310 — resume scoring filters to active job statuses.

Pre-fix the rescore picked every job in a relevant cluster
regardless of status, so EXPIRED + ARCHIVED rows got scored too —
wasted compute, and the user's "Top Matches" panel could surface
jobs they couldn't actually apply to.

F310 mirrors the active-statuses set
(``"new", "under_review", "accepted"``) used by
``maintenance_task.rescore_jobs`` /
``reclassify_and_rescore``. Combined with F300's UPSERT-then-clean
cleanup pass, ResumeScore rows for jobs that flipped to expired/
archived since the last run get pruned automatically.
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
os.environ.setdefault("JWT_SECRET", "pytest-f310")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "workers" / "tasks" / "resume_score_task.py").read_text()


def test_jobs_query_filters_active_statuses():
    """The jobs SELECT must filter on ``Job.status.in_(...)`` —
    expired and archived jobs shouldn't get re-scored.
    """
    src = _read()
    handler_start = src.find("def score_resume_task(")
    handler_end = src.find("@celery_app.task", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    body = src[handler_start:handler_end]
    assert "Job.status.in_(" in body, (
        "F310 regression: jobs query no longer filters on "
        "Job.status. Expired and archived rows will be re-scored "
        "again — wasted compute + misleading 'Top Matches' UI."
    )


def test_active_statuses_match_rescore_jobs():
    """The status whitelist must match the one used by
    maintenance_task — drift would mean rescore-time and
    score_resume-time disagree on which jobs are 'active'.
    """
    src = _read()
    handler_start = src.find("def score_resume_task(")
    handler_end = src.find("@celery_app.task", handler_start + 1)
    body = src[handler_start:handler_end] if handler_end > 0 else src[handler_start:]
    assert "new" in body and "under_review" in body and "accepted" in body, (
        "F310 regression: ACTIVE_STATUSES set drifted from the "
        "maintenance_task convention. Rescore tasks will see a "
        "different 'active' set than score_resume."
    )


def test_query_keeps_role_cluster_filter():
    """The role_cluster filter (relevant clusters only) must stay —
    F310 ADDS a status filter, doesn't replace the cluster filter.
    """
    src = _read()
    handler_start = src.find("def score_resume_task(")
    handler_end = src.find("@celery_app.task", handler_start + 1)
    body = src[handler_start:handler_end] if handler_end > 0 else src[handler_start:]
    assert "Job.role_cluster.in_(relevant_clusters)" in body, (
        "F310 regression: role_cluster filter removed. Score "
        "scope expanded to the entire jobs table — wildly wrong."
    )
