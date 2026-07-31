"""F356 — every beat-scheduled task must be a registered Celery task.

Root cause of the AI-insights outage: ``run_weekly_insights`` (and
``run_backup``, ``auto_probe_recent_funding``) were listed in
``celery_app.conf.beat_schedule`` but their modules were never
imported in ``app/workers/tasks/__init__.py``. Since
``autodiscover_tasks(["app.workers.tasks"])`` resolves to a
non-existent ``app.workers.tasks.tasks`` module, task registration
depends entirely on those explicit imports — so a beat entry whose
module isn't imported fires on schedule and the worker rejects it
with "Received unregistered task" (KeyError). Silent: no user-facing
error, the feature just never produces anything.

This test closes the whole class: it asserts that every ``task``
named in the beat schedule is present in ``celery_app.tasks``. Same
spirit as the F345 "every PlatformFilter value has a fetcher"
invariant.
"""

from __future__ import annotations

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
os.environ.setdefault("JWT_SECRET", "pytest-beat")


def _load_celery():
    # Importing the tasks package runs the decorators that register
    # every task — exactly what the worker does at startup.
    import app.workers.tasks  # noqa: F401
    from app.workers.celery_app import celery_app

    return celery_app


def test_every_beat_task_is_registered():
    celery_app = _load_celery()
    registered = set(celery_app.tasks.keys())

    scheduled = {
        entry["task"]
        for entry in celery_app.conf.beat_schedule.values()
        if "task" in entry
    }
    missing = sorted(scheduled - registered)
    assert not missing, (
        "Beat schedules these tasks but they are NOT registered on the "
        f"worker (unregistered-task KeyError every firing): {missing}. "
        "Import their module in app/workers/tasks/__init__.py."
    )


def test_the_three_regressed_tasks_are_registered():
    """Belt-and-braces on the specific tasks the outage hit, so a
    future refactor that drops one is caught by name."""
    celery_app = _load_celery()
    for name in (
        "app.workers.tasks.ai_insights_task.run_weekly_insights",
        "app.workers.tasks.backup_task.run_backup",
        "app.workers.tasks.funding_followup_task.auto_probe_recent_funding",
    ):
        assert name in celery_app.tasks, f"{name} not registered"
