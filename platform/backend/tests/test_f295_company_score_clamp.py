"""F295 — clamp company_score to ≤100 (closes F213).

F213 found ``GET /companies/scores`` returned values >100 for
companies with more global-remote OUT-OF-CLUSTER jobs than
in-cluster jobs. Live verified: Supabase=151.9, GitLab=111.8,
Coalition=104.6.

Root cause: asymmetric denominators in the ``remote_ratio``
component. ``remote_jobs`` counted ALL of a company's jobs with
``geography_bucket='global_remote'``; ``relevant_jobs`` counted
only ``role_cluster IN (infra, security)``. A company with 9
in-cluster + 46 out-of-cluster-but-remote produced ``remote/
relevant = 46/9 = 5.11`` × 20 = 102.22, blowing past the
documented 20-point cap on that component.

F295 fixes the semantic intent: ``remote_jobs`` now counts only
jobs that are BOTH ``global_remote`` AND in a relevant cluster,
matching the ``relevant_jobs`` denominator. As defense-in-depth
the Python formula also clamps the ratio at 1.0 so any future
SQL refactor that breaks the structural invariant doesn't leak
through.
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
os.environ.setdefault("JWT_SECRET", "pytest-f295")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "companies.py").read_text()


def test_remote_jobs_count_filters_to_relevant_clusters():
    """The SQL ``remote_jobs`` aggregation must filter on the
    same cluster set as ``relevant_jobs`` so the denominators
    align — that's the semantic intent of the metric ("fraction
    of relevant jobs that are global-remote") and prevents
    >100 scores at the source.
    """
    src = _read()
    handler_start = src.find("async def company_scores")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    # The remote_jobs aggregation must AND the cluster filter.
    # We look for the structural marker — the ``role_cluster.in_(relevant)``
    # check inside the remote_jobs case expression.
    remote_jobs_idx = handler.find('"remote_jobs"')
    assert remote_jobs_idx > 0, (
        "F295 regression: ``remote_jobs`` label was renamed or "
        "removed."
    )
    # Slice a window around the remote_jobs aggregation
    window_start = max(0, remote_jobs_idx - 500)
    window = handler[window_start:remote_jobs_idx + 100]
    assert "role_cluster.in_(relevant)" in window, (
        "F295 regression: ``remote_jobs`` aggregation no longer "
        "filters on the relevant-cluster set. Asymmetric "
        "denominators reopen — companies with out-of-cluster "
        "global-remote jobs will inflate company_score past 100 "
        "again."
    )


def test_python_formula_clamps_remote_ratio():
    """As defense-in-depth the Python formula must clamp
    ``remote / relevant`` at 1.0 before multiplying by 20.
    Even if a future SQL refactor breaks the structural invariant,
    the Python clamp keeps company_score ≤ 100.
    """
    src = _read()
    handler_start = src.find("async def company_scores")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "min(remote / max(relevant, 1), 1.0) * 20" in handler, (
        "F295 regression: the Python ``remote_ratio`` formula no "
        "longer clamps at 1.0. SQL invariant is the only defense; "
        "any refactor that breaks the in-cluster filter on "
        "remote_jobs will produce >100 scores again."
    )


def test_documentation_invariant():
    """The companies.py file should have a comment that documents
    the ≤100 ceiling as load-bearing, so a future contributor
    knows the invariant matters before they refactor.
    """
    src = _read()
    handler_start = src.find("async def company_scores")
    handler_end = src.find("@router.", handler_start + 1)
    handler = src[handler_start:handler_end]
    assert "F213" in handler or "F295" in handler, (
        "F295 regression: the F213/F295 context comment was "
        "removed. Future contributors won't know the SQL/Python "
        "double-defense is intentional."
    )
