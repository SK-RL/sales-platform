"""F307 — thread approved_roles_set into scan-time scoring (data reliability).

Pre-fix scan_task.py:_upsert_job called compute_relevance_score
WITHOUT ``approved_roles_set``, so the scorer fell back to the
hardcoded ``INFRA_ROLES + SECURITY_ROLES + QA_ROLES`` superset.

Meanwhile rescore_jobs / reclassify_and_rescore (in
maintenance_task.py) build ``approved_roles_set`` from the admin's
DB-stored ``RoleClusterConfig.approved_roles`` and pass it through.

Net effect: a freshly-ingested job's score diverged from what the
next nightly rescore would compute. Admin-configured approved-role
lists were silently ignored at scan time.

F307 threads ``approved_roles_set`` through:
  ``_scan_board`` → ``_upsert_job`` → ``compute_relevance_score``

Built once per scan-task invocation via the
``_approved_roles_set_from_config`` helper that mirrors the
construction in maintenance_task.
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
os.environ.setdefault("JWT_SECRET", "pytest-f307")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "workers" / "tasks" / "scan_task.py").read_text()


def test_helper_builds_approved_roles_set():
    """The shared helper builds the same flat lowercase set that
    maintenance_task constructs inline. Prevents copy-paste drift."""
    from app.workers.tasks.scan_task import _approved_roles_set_from_config

    config = {
        "infra": {"approved_roles": ["DevOps Engineer", "SRE"]},
        "security": {"approved_roles": ["SOC Analyst"]},
        "qa": {"approved_roles": []},
    }
    result = _approved_roles_set_from_config(config)
    assert result == {"devops engineer", "sre", "soc analyst"}, (
        f"F307 regression: helper returned {result!r}; expected the "
        f"three lowercased role names from infra+security."
    )


def test_helper_handles_empty_config():
    from app.workers.tasks.scan_task import _approved_roles_set_from_config

    assert _approved_roles_set_from_config(None) == set()
    assert _approved_roles_set_from_config({}) == set()


def test_upsert_job_signature_accepts_approved_roles_set():
    """The handler must accept ``approved_roles_set`` as a kwarg —
    that's how the F307 plumbing threads it through.
    """
    src = _read()
    sig_start = src.find("def _upsert_job(")
    sig_end = src.find(") -> str:", sig_start)
    sig = src[sig_start:sig_end]
    assert "approved_roles_set" in sig, (
        "F307 regression: ``_upsert_job`` no longer accepts "
        "``approved_roles_set``. Scan-time scoring will diverge "
        "from rescore-time scoring again."
    )


def test_compute_relevance_score_calls_pass_approved_roles_set():
    """Both scoring sites in ``_upsert_job`` (existing-job branch
    and new-job branch) must pass ``approved_roles_set`` to
    ``compute_relevance_score``.
    """
    src = _read()
    body_start = src.find("def _upsert_job(")
    body_end = src.find("\ndef _scan_board", body_start)
    body = src[body_start:body_end]
    # Count compute_relevance_score calls — should be 2 (existing + new)
    score_call_count = body.count("compute_relevance_score(")
    assert score_call_count == 2, (
        f"F307 regression: expected 2 compute_relevance_score "
        f"calls in _upsert_job (existing-job + new-job branches), "
        f"found {score_call_count}."
    )
    # Each call must pass approved_roles_set
    approved_kwarg_count = body.count("approved_roles_set=approved_roles_set")
    assert approved_kwarg_count == 2, (
        f"F307 regression: only {approved_kwarg_count} of 2 "
        f"compute_relevance_score calls pass "
        f"``approved_roles_set=approved_roles_set``. Both must "
        f"pass it or scan-time scoring will partially diverge."
    )


def test_scan_board_threads_approved_roles_set():
    """``_scan_board`` must accept and pass through
    ``approved_roles_set`` so the scoring layer below sees it.
    """
    src = _read()
    sig_start = src.find("def _scan_board(")
    sig_end = src.find(") -> dict:", sig_start)
    sig = src[sig_start:sig_end]
    assert "approved_roles_set" in sig, (
        "F307 regression: ``_scan_board`` no longer accepts "
        "``approved_roles_set``. The thread breaks here."
    )
    # And the inner _upsert_job call must pass it through
    body_start = src.find("def _scan_board(")
    body_end = src.find("\n@celery_app.task", body_start)
    body = src[body_start:body_end]
    assert "approved_roles_set=approved_roles_set" in body, (
        "F307 regression: ``_scan_board`` no longer threads "
        "``approved_roles_set`` into ``_upsert_job``."
    )


def test_celery_tasks_build_approved_roles_set():
    """Each celery task that loads cluster_config must also build
    the approved_roles_set via the helper. Without this the
    threading is dead-ended."""
    src = _read()
    # Every cluster_config load should be followed by a call to
    # _approved_roles_set_from_config.
    config_loads = src.count("cluster_config = load_cluster_config_sync(session)")
    helper_uses = src.count("_approved_roles_set_from_config(cluster_config)")
    assert config_loads == helper_uses, (
        f"F307 regression: {config_loads} cluster_config loads but "
        f"only {helper_uses} ``_approved_roles_set_from_config`` "
        f"calls. Some celery tasks load the config but don't "
        f"build the role set, so their scans use the hardcoded "
        f"fallback again."
    )
