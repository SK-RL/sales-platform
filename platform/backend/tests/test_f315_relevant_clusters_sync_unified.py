"""F315 — single source of truth for ``get_relevant_clusters_sync``.

After F313 added the helper to ``_role_matching.py`` and F314
adopted it in ``question_collection_task.py``, the
``resume_score_task.py`` still had its own duplicate
``_get_relevant_clusters_sync`` with the same query shape.

F315 collapses to a single canonical helper imported by every
sync caller. Drift across the duplicates was a latent risk: if
the fallback contract ever changes (e.g. expanding the default
pair beyond ``["infra", "security"]``), only one place gets the
update and the others silently lag.

This test pins the invariant so a future contributor doesn't
re-introduce a parallel helper.
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
os.environ.setdefault("JWT_SECRET", "pytest-f315")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_only_one_def_exists():
    """The canonical helper lives in ``_role_matching.py``. No
    other backend file should declare its own
    ``def get_relevant_clusters_sync`` or
    ``def _get_relevant_clusters_sync`` body — drift across
    duplicates is the bug F315 closed.
    """
    backend = _BACKEND / "app"
    canonical = backend / "workers" / "tasks" / "_role_matching.py"
    canonical_def = "def get_relevant_clusters_sync"
    found_in: list[str] = []
    for path in backend.rglob("*.py"):
        try:
            text = path.read_text()
        except Exception:
            continue
        # Match either the public or the legacy underscore name.
        if (
            "def get_relevant_clusters_sync" in text
            or "def _get_relevant_clusters_sync" in text
        ):
            found_in.append(str(path.relative_to(backend)))
    # The canonical file MUST be in the list.
    assert "workers/tasks/_role_matching.py" in found_in, (
        "F315 regression: canonical helper missing from "
        "_role_matching.py."
    )
    # And nothing else.
    extras = [p for p in found_in if p != "workers/tasks/_role_matching.py"]
    assert not extras, (
        f"F315 regression: duplicate ``get_relevant_clusters_sync`` "
        f"definitions found in: {extras}. The whole point of F315 "
        f"was to keep ONE helper so the fallback contract can't "
        f"drift across files."
    )


def test_resume_score_task_imports_from_canonical():
    """resume_score_task.py must import the helper from
    ``_role_matching`` rather than redefining it.
    """
    src = (_BACKEND / "app" / "workers" / "tasks" / "resume_score_task.py").read_text()
    assert "from app.workers.tasks._role_matching import get_relevant_clusters_sync" in src, (
        "F315 regression: resume_score_task no longer imports the "
        "canonical helper. A drifted local copy is back."
    )
    # The canonical name should be CALLED in the handler.
    assert "get_relevant_clusters_sync(session)" in src
