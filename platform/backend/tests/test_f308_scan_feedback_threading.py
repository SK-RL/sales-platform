"""F308 — thread feedback_adjustment into scan-time scoring.

Same data-reliability class as F307. Pre-fix scan_task didn't load
the ``ScoringSignal`` table at all, so newly-ingested jobs scored
without any feedback adjustment. The next nightly rescore would
correct them, but in the window between scan and rescore the
review-queue / dashboard / pipeline showed inflated scores for
jobs from heavily-downvoted (cluster, company, geo, level)
combinations.

F308 plumbs ``signals_cache`` through:
  ``_scan_board`` → ``_upsert_job`` → ``compute_relevance_score``

Loaded once per celery-task entrypoint via
``_load_signals_cache_sync``, mirroring the pattern in
maintenance_task.rescore_jobs.
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
os.environ.setdefault("JWT_SECRET", "pytest-f308")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "workers" / "tasks" / "scan_task.py").read_text()


def test_signals_cache_helper_exists():
    """``_load_signals_cache_sync`` builds the same {signal_key:
    weight} dict that maintenance_task.rescore_jobs constructs.
    Shared helper means scan-time + rescore-time can't drift on
    cache shape.
    """
    src = _read()
    assert "def _load_signals_cache_sync" in src, (
        "F308 regression: ``_load_signals_cache_sync`` helper "
        "removed. Without it, scan-time scoring won't know about "
        "feedback signals."
    )


def test_upsert_job_accepts_signals_cache():
    src = _read()
    sig_start = src.find("def _upsert_job(")
    sig_end = src.find(") -> str:", sig_start)
    sig = src[sig_start:sig_end]
    assert "signals_cache" in sig, (
        "F308 regression: ``_upsert_job`` no longer accepts "
        "``signals_cache``. Feedback signals can't reach the "
        "scoring layer."
    )


def test_scan_board_threads_signals_cache():
    src = _read()
    sig_start = src.find("def _scan_board(")
    sig_end = src.find(") -> dict:", sig_start)
    sig = src[sig_start:sig_end]
    assert "signals_cache" in sig, (
        "F308 regression: ``_scan_board`` no longer accepts "
        "``signals_cache``."
    )
    body_start = src.find("def _scan_board(")
    body_end = src.find("\n@celery_app.task", body_start)
    body = src[body_start:body_end]
    assert "signals_cache=signals_cache" in body, (
        "F308 regression: ``_scan_board`` no longer threads "
        "``signals_cache`` into ``_upsert_job``."
    )


def test_upsert_job_calls_get_feedback_adjustment():
    """Both scoring branches in _upsert_job (existing-job + new-job)
    must call ``get_feedback_adjustment`` and pass the result to
    ``compute_relevance_score``.
    """
    src = _read()
    body_start = src.find("def _upsert_job(")
    body_end = src.find("\ndef _scan_board", body_start)
    body = src[body_start:body_end]
    feedback_calls = body.count("get_feedback_adjustment(")
    assert feedback_calls == 2, (
        f"F308 regression: expected 2 ``get_feedback_adjustment`` "
        f"calls in _upsert_job (existing + new branches); found "
        f"{feedback_calls}."
    )
    feedback_kwarg_count = body.count("feedback_adjustment=feedback_adj_")
    assert feedback_kwarg_count == 2, (
        f"F308 regression: only {feedback_kwarg_count} of 2 "
        f"compute_relevance_score calls pass "
        f"``feedback_adjustment``. Both must pass it or scan-time "
        f"scores will partially diverge from rescore."
    )


def test_celery_tasks_build_signals_cache():
    """Every celery scan task must load signals_cache via the
    helper after loading cluster_config. Otherwise the threading
    is dead-ended.
    """
    src = _read()
    config_loads = src.count("cluster_config = load_cluster_config_sync(session)")
    cache_loads = src.count("_load_signals_cache_sync(session)")
    assert config_loads == cache_loads, (
        f"F308 regression: {config_loads} cluster_config loads but "
        f"only {cache_loads} ``_load_signals_cache_sync`` calls. "
        f"Some celery tasks load the config but skip the signals "
        f"cache, leaving feedback signals unapplied at scan time."
    )


def test_signals_cache_optional_for_unit_tests():
    """When ``signals_cache=None`` the call to
    ``get_feedback_adjustment`` is short-circuited so the helper
    doesn't crash on missing ``ScoringSignal`` rows. This keeps
    backwards-compat with any external test harness that builds
    Job rows without going through the scan path.
    """
    src = _read()
    body_start = src.find("def _upsert_job(")
    body_end = src.find("\ndef _scan_board", body_start)
    body = src[body_start:body_end]
    # The existing-branch and new-branch should each have a
    # ``signals_cache else 0.0`` short-circuit.
    short_circuit_count = body.count("if signals_cache else 0.0")
    assert short_circuit_count >= 2, (
        f"F308 regression: short-circuit on ``signals_cache=None`` "
        f"missing in {2 - short_circuit_count} of 2 branches. "
        f"Tests that don't supply a signals_cache will crash."
    )
