"""Feedback ticket 14d00e33 — "Add a column which shows that I have
already applied for this job."

The All-Jobs list now enriches every row with the requesting user's
application status so the UI can render an "Applied" marker. This is
the same enrichment shape as ``resume_score`` (batch query keyed by
the page's job_ids, then folded into the serialized dict), so the
lock here is source-level in the same spirit as the rest of the
suite (no live DB): prove the query exists, is user-scoped, and that
the enriched key ships on the response dict.

Backend regression risks this guards:
  * Someone removes the Application enrichment → All Jobs silently
    loses the marker (the exact "silent feature loss" class as F356).
  * Someone scopes the query to the wrong user or drops the
    job_ids filter → cross-user leak / N+1.
"""

from __future__ import annotations

import inspect
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-applied-marker")


def _list_jobs_src() -> str:
    from app.api.v1.jobs import list_jobs

    return inspect.getsource(list_jobs)


def test_application_model_imported():
    """The enrichment needs the model in scope."""
    import app.api.v1.jobs as jobs_mod

    from app.models.application import Application

    assert getattr(jobs_mod, "Application", None) is Application


def test_list_jobs_enriches_application_status():
    src = _list_jobs_src()
    # The serialized row must carry the key the frontend reads.
    assert 'd["application_status"]' in src, (
        "GET /jobs no longer enriches rows with application_status — "
        "All Jobs loses the 'already applied' marker (ticket 14d00e33)."
    )
    # And it must come from an Application lookup, not a stray literal.
    assert "Application.status" in src
    assert "Application.job_id" in src


def test_application_lookup_is_user_scoped_and_batched():
    """User scoping prevents leaking another user's application state;
    the ``job_id.in_(job_ids)`` bound keeps it one query per page."""
    src = _list_jobs_src()
    assert "Application.user_id == user.id" in src, (
        "Application enrichment must filter by the requesting user — "
        "otherwise All Jobs would show other users' applications."
    )
    assert "Application.job_id.in_(job_ids)" in src, (
        "Application enrichment must be batched over the page's "
        "job_ids, not fetched per-row (N+1)."
    )
