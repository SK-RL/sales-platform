"""F314 — collect_questions filters to relevant clusters.

Sibling to F313. Pre-fix the question-prefetch task used
``Job.role_cluster != ""`` to filter to "relevant" jobs but the
predicate counted ANY classified cluster — including admin-added
clusters marked ``is_relevant=False`` for analytics-only tracking.
The collector wasted ATS-API calls fetching application questions
for jobs the user would never apply to.

F314 swaps the predicate to
``Job.role_cluster.in_(get_relevant_clusters_sync(session))`` so
the collector matches the same relevant-set used by
/jobs?role_cluster=relevant, /companies/scores, and the F313
auto_target_companies fix.
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
os.environ.setdefault("JWT_SECRET", "pytest-f314")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_collect_questions_uses_relevant_clusters_helper():
    """The handler must call ``get_relevant_clusters_sync`` and
    use the result in the WHERE clause.
    """
    src = (_BACKEND / "app" / "workers" / "tasks" / "question_collection_task.py").read_text()
    assert "get_relevant_clusters_sync(session)" in src, (
        "F314 regression: collect_questions no longer calls "
        "``get_relevant_clusters_sync``. The relevant-cluster "
        "filter is back to the naive any-non-empty form."
    )
    assert "Job.role_cluster.in_(relevant_clusters)" in src, (
        "F314 regression: WHERE clause no longer uses ``in_`` on "
        "the relevant_clusters list. Could be filtering on the "
        "wrong predicate now."
    )


def test_collect_questions_no_longer_uses_naive_filter():
    """The pre-fix anti-pattern ``Job.role_cluster != ""`` shouldn't
    come back."""
    src = (_BACKEND / "app" / "workers" / "tasks" / "question_collection_task.py").read_text()
    # Find handler body
    handler_start = src.find("def collect_questions(")
    handler_end = src.find("@celery_app.task", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    body = src[handler_start:handler_end]
    # The naive filter shouldn't be in the LIVE handler body —
    # but the comment ABOVE the WHERE clause documents it as
    # "pre-fix"; that's fine. Strip comment lines first.
    code_only = "\n".join(
        ln for ln in body.splitlines() if not ln.strip().startswith("#")
    )
    assert 'Job.role_cluster != ""' not in code_only, (
        "F314 regression: naive ``role_cluster != \"\"`` filter "
        "is back in the executed code. Non-relevant clusters will "
        "be re-included again."
    )
