"""F313 — auto_target_companies filters to RELEVANT clusters only.

Pre-fix the qualifying-companies query used
``role_cluster != "" AND role_cluster IS NOT NULL`` — counting ANY
classified job toward the 2+ threshold. Admins who added a cluster
like ``data`` with ``is_relevant=False`` (e.g. for analytics-only
tracking) had those jobs flip companies to ``is_target=True``
against intent.

F313 ships a sync mirror of the async ``_get_relevant_clusters``
helper (``get_relevant_clusters_sync``) and uses it in
``auto_target_companies`` so the auto-target task counts only
relevant-cluster jobs — same source of truth as
/jobs?role_cluster=relevant and /companies/scores.
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
os.environ.setdefault("JWT_SECRET", "pytest-f313")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_relevant_clusters_sync_helper_exists():
    """The sync helper that mirrors the async
    ``_get_relevant_clusters`` must exist so Celery tasks can
    use the same source of truth as the API.
    """
    from app.workers.tasks._role_matching import get_relevant_clusters_sync
    # Just verify the symbol is importable. Behaviour-test would
    # need a DB session.
    assert callable(get_relevant_clusters_sync)


def test_auto_target_uses_relevant_clusters():
    """``auto_target_companies`` must filter via
    ``role_cluster.in_(relevant_clusters)`` — not
    ``role_cluster != ""``.
    """
    src = (_BACKEND / "app" / "workers" / "tasks" / "maintenance_task.py").read_text()
    handler_start = src.find("def auto_target_companies(")
    handler_end = src.find("@celery_app.task", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    body = src[handler_start:handler_end]
    assert "get_relevant_clusters_sync(session)" in body, (
        "F313 regression: auto_target_companies no longer calls "
        "``get_relevant_clusters_sync``. Auto-target will count "
        "non-relevant clusters again."
    )
    assert "Job.role_cluster.in_(relevant_clusters)" in body, (
        "F313 regression: auto_target query no longer filters by "
        "the relevant-clusters list."
    )
    # Pre-fix anti-pattern that should NOT come back
    assert 'Job.role_cluster != ""' not in body, (
        "F313 regression: auto_target_companies is back to the "
        "non-empty-cluster filter, which counts non-relevant "
        "clusters too."
    )


def test_active_status_filter_kept():
    """F313 keeps the active-statuses filter (auto-target should
    only count active jobs, not expired)."""
    src = (_BACKEND / "app" / "workers" / "tasks" / "maintenance_task.py").read_text()
    handler_start = src.find("def auto_target_companies(")
    handler_end = src.find("@celery_app.task", handler_start + 1)
    body = src[handler_start:handler_end] if handler_end > 0 else src[handler_start:]
    assert "Job.status.in_(active_statuses)" in body
