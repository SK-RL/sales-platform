"""Structural tests for F262 — Celery OOM hardening.

No live DB / no celery worker. We assert the invariants that prove
the OOM fix is wired correctly:

  1. Heavy batch tasks (``rescore_jobs``, ``reclassify_and_rescore``)
     are routed to the ``heavy`` queue. A regression where someone
     drops the route would put them back on the default queue, where
     the 1.5GB worker would OOM during the chunked run (each chunk
     allocates ~50MB; under load the default worker shouldn't be
     juggling that AND scan tasks).

  2. The chunked-iteration code in ``rescore_jobs`` doesn't call
     ``select(Job).all()`` on the unbounded set. We can't easily run
     the task without a real DB, but we CAN inspect its source for
     the patterns we care about (presence of LIMIT/keyset paging,
     presence of ``expire_all()``).

  3. The default queue and heavy queue routes don't overlap — every
     task with an explicit route goes to exactly one queue.

End-to-end memory profiling under load is covered by the deploy-time
verification (run the task, watch ``docker stats`` for flat memory).
"""
from __future__ import annotations

import inspect
import os


# Minimum env so app.config imports cleanly (mirrors other test modules).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-celery-oom")


# ── Queue routing: heavy tasks go to the heavy queue ─────────────


def test_rescore_jobs_routed_to_heavy_queue():
    """``rescore_jobs`` must route to ``heavy``. If a future maintainer
    drops the route, the task lands on the default queue and the 1.5GB
    default worker has to absorb both the chunked rescore AND its
    in-flight scan workload — possible OOM on busy nights.
    """
    from app.workers.celery_app import celery_app

    routes = celery_app.conf.task_routes or {}
    rescore_route = routes.get(
        "app.workers.tasks.maintenance_task.rescore_jobs"
    )
    assert rescore_route == {"queue": "heavy"}, (
        f"rescore_jobs route is {rescore_route!r}; expected "
        "{'queue': 'heavy'}. The heavy worker container is the only "
        "one with the 2GB ceiling needed for this batch task."
    )


def test_reclassify_and_rescore_routed_to_heavy_queue():
    """Same invariant for ``reclassify_and_rescore``. This is admin-
    triggered (button on /monitoring) so it doesn't fire on a cron,
    but if anyone clicks it on the wrong worker the OOM blast radius
    is the everyday scan worker — exactly the failure mode the queue
    split exists to prevent.
    """
    from app.workers.celery_app import celery_app

    routes = celery_app.conf.task_routes or {}
    reclassify_route = routes.get(
        "app.workers.tasks.maintenance_task.reclassify_and_rescore"
    )
    assert reclassify_route == {"queue": "heavy"}, (
        f"reclassify_and_rescore route is {reclassify_route!r}; "
        "expected {'queue': 'heavy'}."
    )


def test_default_queue_set_explicitly():
    """``task_default_queue`` must be set so unrouted tasks land on
    the everyday worker. Pre-fix, celery's implicit default is the
    queue named ``celery`` — but we run workers with explicit
    ``--queues=default`` and ``--queues=heavy``, so any task without
    an explicit route on an implicit ``celery`` queue would go
    nowhere (sit in Redis forever). Setting it here closes that gap.
    """
    from app.workers.celery_app import celery_app

    assert celery_app.conf.task_default_queue == "default", (
        "task_default_queue must be 'default' to match the worker's "
        "--queues argument. Otherwise unrouted tasks publish to a "
        "queue no worker is consuming."
    )


# ── Chunked iteration: heavy tasks don't load all jobs at once ────


def test_rescore_jobs_uses_chunked_iteration():
    """The fixed task must use bounded-memory iteration. We grep the
    source for the markers that distinguish it from the pre-fix
    ``select(Job).all()`` shape:
      * ``_RESCORE_CHUNK`` constant (the chunk size) — only present
        in the chunked version.
      * ``expire_all()`` between chunks — drops ORM identity-map
        state so memory stays flat across iterations.
      * ``last_id`` keyset variable — pre-fix had no pagination at
        all; OFFSET-based would use ``offset`` so this catches both
        regressions (back to all-at-once OR back to OFFSET).
    A regression that drops any of these markers either re-introduces
    the OOM (no chunking) or makes the task progressively slower
    (OFFSET on Postgres scans skipped rows on every chunk).
    """
    from app.workers.tasks import maintenance_task

    src = inspect.getsource(maintenance_task.rescore_jobs)
    assert "_RESCORE_CHUNK" in src, (
        "rescore_jobs is no longer using the _RESCORE_CHUNK constant. "
        "Did someone revert F262's chunked iteration? "
        "Pre-fix code OOM-killed the worker every night at 03:01 UTC."
    )
    assert "expire_all" in src, (
        "rescore_jobs is missing session.expire_all() between chunks. "
        "Without it the ORM identity map accumulates and we're back "
        "to ~500MB resident — same OOM shape as before F262."
    )
    assert "last_id" in src, (
        "rescore_jobs lost its keyset paging variable. Either it's "
        "loading everything at once again, or it's using OFFSET "
        "paging which degrades quadratically on Postgres."
    )


def test_reclassify_and_rescore_uses_chunked_iteration():
    """Same chunked-iteration invariant for ``reclassify_and_rescore``.
    This task is the bigger sibling — same data scan as ``rescore_jobs``
    plus role-matching + geography classification per row — so any
    regression here is even more memory-painful.
    """
    from app.workers.tasks import maintenance_task

    src = inspect.getsource(maintenance_task.reclassify_and_rescore)
    assert "_RESCORE_CHUNK" in src, (
        "reclassify_and_rescore lost the chunked-iteration pattern. "
        "Restore F262's keyset loop so the task can't OOM the heavy "
        "worker."
    )
    assert "expire_all" in src
    assert "last_id" in src


def test_chunk_size_is_reasonable():
    """The chunk size is an empirical knob — too small and we pay
    round-trip overhead per chunk; too large and we risk exceeding
    the per-chunk memory budget. 2000 was chosen to keep peak ORM
    state under ~50MB. Lock down the order of magnitude here so a
    careless edit (``_RESCORE_CHUNK = 200000``) doesn't quietly
    re-introduce the OOM shape.
    """
    from app.workers.tasks.maintenance_task import _RESCORE_CHUNK

    assert 100 <= _RESCORE_CHUNK <= 10000, (
        f"_RESCORE_CHUNK = {_RESCORE_CHUNK} is outside the safe "
        "range [100, 10000]. Tighten it back toward 2000 — that's "
        "the value F262 validated against the 86k-row prod dataset "
        "without OOMing the 2GB heavy worker."
    )


# ═══ F355 — worker child recycling ═══════════════════════════════


def test_worker_max_memory_per_child_set_and_safe_for_concurrency():
    """The leaking default worker must recycle its child before the
    cgroup OOM-kills it. The per-child threshold is sized for
    concurrency=2 under the 2048 MiB container limit — a value near
    the container limit would never fire (2 children can't both reach
    it)."""
    from app.workers.celery_app import celery_app

    kib = celery_app.conf.worker_max_memory_per_child
    assert kib, "worker_max_memory_per_child not set — leaking worker won't recycle"
    mib = kib / 1024
    # Must leave room for 2 concurrent children + parent under 2048 MiB.
    # 2×mib + 130 parent < 2048  →  mib < 959.
    assert mib < 959, (
        f"per-child threshold {mib:.0f} MiB too high for concurrency=2 "
        f"under the 2048 MiB limit — 2 children would OOM before recycle."
    )
    # And high enough that recycling isn't pathologically frequent.
    assert mib > 400, f"per-child threshold {mib:.0f} MiB is wastefully low"


def test_worker_max_tasks_per_child_backstop():
    from app.workers.celery_app import celery_app

    n = celery_app.conf.worker_max_tasks_per_child
    assert n and 50 <= n <= 1000, (
        "worker_max_tasks_per_child should be a modest backstop (50–1000)"
    )


def test_prod_worker_limit_covers_recycle_threshold():
    """Guard the compose limit vs the recycle math so they can't drift
    apart: 2 × per-child + parent must fit the container limit."""
    import re
    from pathlib import Path

    from app.workers.celery_app import celery_app

    compose = (
        Path(__file__).resolve().parent.parent.parent / "docker-compose.prod.yml"
    ).read_text()
    # First `--queues=default` worker block's memory limit.
    default_block = compose.split("--queues=default", 1)[1]
    m = re.search(r"memory:\s*(\d+)M", default_block)
    assert m, "couldn't find default worker memory limit"
    limit_mib = int(m.group(1))

    per_child_mib = celery_app.conf.worker_max_memory_per_child / 1024
    # concurrency default is 2; parent ~130 MiB.
    worst_case = 2 * per_child_mib + 130
    assert worst_case < limit_mib, (
        f"2 recycled children ({worst_case:.0f} MiB) exceed the "
        f"{limit_mib} MiB container limit — raise the limit or lower "
        f"the per-child threshold."
    )
