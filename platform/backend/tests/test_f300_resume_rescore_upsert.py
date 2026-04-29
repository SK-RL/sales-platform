"""F300 — resume rescore UPSERT-then-clean (closes F105).

F105 found ``POST /resume/{id}/score`` used delete-old-then-rescore
semantics: every rescore deleted ALL existing ResumeScore rows
up-front, leaving the UI showing ``jobs_scored=0`` for the full
~90s rescore window. The user's "Best Score" / "Top Matches"
panels went blank during a rescore — UX-regressive.

F300 swaps the order:
  1. Score each job → UPSERT (the existing on_conflict_do_update
     path, no change needed there).
  2. Track the set of job_ids touched in the current run.
  3. After the loop, DELETE any ResumeScore rows for THIS resume
     whose job_id is NOT in the touched set — cleanup of jobs
     that were relevant in the old run but aren't anymore.

Result: old scores stay visible (slightly stale) throughout the
rescore window, gradually overwritten with new numbers. No blank
screen. Worker crash mid-rescore leaves the user with old +
partial-new scores which is strictly better than pre-fix's
all-blank + partial-new.
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
os.environ.setdefault("JWT_SECRET", "pytest-f300")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "workers" / "tasks" / "resume_score_task.py").read_text()


def test_no_upfront_delete_loop_for_old_scores():
    """The pre-fix anti-pattern was a ``for old in old_scores:
    session.delete(old)`` loop run BEFORE the rescore loop. The
    fix removed that loop. A regression that re-introduces
    upfront deletion is the whole user-visible bug F105
    documented.
    """
    src = _read()
    # Slice the score_resume_task body
    start = src.find("def score_resume_task(")
    end = src.find("@celery_app.task", start + 1)
    if end < 0:
        end = len(src)
    body = src[start:end]
    # Anti-pattern: ``session.delete(old)`` inside a for loop
    # over a pre-rescore SELECT result.
    assert "for old in old_scores" not in body, (
        "F300 regression: upfront delete-old loop is back. The "
        "rescore window will leave users with blank scores again."
    )
    assert "session.delete(old)" not in body, (
        "F300 regression: upfront ``session.delete(old)`` call is "
        "back. Same blank-screen UX as F105 documented."
    )


def test_tracks_scored_job_ids_for_final_cleanup():
    """The new shape requires tracking which job_ids were touched
    so the final cleanup pass can remove only stale rows.
    """
    src = _read()
    start = src.find("def score_resume_task(")
    end = src.find("@celery_app.task", start + 1)
    if end < 0:
        end = len(src)
    body = src[start:end]
    assert "scored_job_ids" in body, (
        "F300 regression: ``scored_job_ids`` set tracking removed. "
        "The final cleanup pass can't run without it."
    )
    assert "scored_job_ids.add(job.id)" in body, (
        "F300 regression: per-job add to scored_job_ids removed. "
        "Cleanup will think NO jobs were scored and delete "
        "everything (the F105 anti-pattern in a different form)."
    )


def test_final_cleanup_uses_notin_filter():
    """The cleanup pass must be a single batched
    ``DELETE ... WHERE resume_id = X AND job_id NOT IN scored_job_ids``
    — not a per-row delete (DoS) and not a delete-everything (F105).
    """
    src = _read()
    start = src.find("def score_resume_task(")
    end = src.find("@celery_app.task", start + 1)
    if end < 0:
        end = len(src)
    body = src[start:end]
    assert "ResumeScore.job_id.notin_(scored_job_ids)" in body, (
        "F300 regression: cleanup no longer filters via "
        "``job_id.notin_(scored_job_ids)``. Either everything "
        "gets deleted (F105 reopens) or nothing gets cleaned up "
        "(stale rows accumulate forever)."
    )


def test_cleanup_returns_stale_pruned_count():
    """Operational visibility — the task return dict should
    include the ``stale_pruned`` count so admins can see how
    many rows were cleaned up. Drift indicator: if this number
    is consistently zero, the criteria for "relevant jobs"
    isn't actually changing and the cleanup is pure overhead.
    """
    src = _read()
    start = src.find("def score_resume_task(")
    end = src.find("@celery_app.task", start + 1)
    if end < 0:
        end = len(src)
    body = src[start:end]
    assert "stale_pruned" in body, (
        "F300 regression: the operational visibility key "
        "``stale_pruned`` was dropped from the task return dict."
    )
