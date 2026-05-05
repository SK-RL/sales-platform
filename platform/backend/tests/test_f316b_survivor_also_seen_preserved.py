"""F316b — preserve _also_seen_on across the F316 IntegrityError survivor-update path.

F316's IntegrityError catch routes a fresh-scan observation onto
the surviving active row when the partial UNIQUE
``uq_jobs_active_company_title`` fires. Pre-F316b that path
unconditionally reassigned ``survivor.raw_json = raw_job.get(
"raw_json", {})`` — losing any prior cross-platform sighting
tracking the survivor had accumulated under ``_also_seen_on``.

F316b mirrors the F88 cross-platform-soft-match branch's defense:
read the existing ``_also_seen_on`` BEFORE the reassign, then
re-attach it on top of the new raw_json. Also records the
current platform:external_id sighting so the cross-platform
tracking continues to grow as the role appears on more sources.
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
os.environ.setdefault("JWT_SECRET", "pytest-f316b")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "workers" / "tasks" / "scan_task.py").read_text()


def test_survivor_path_preserves_also_seen_on():
    """The IntegrityError survivor-update path must capture
    ``_also_seen_on`` from the existing raw_json BEFORE the
    reassign and re-attach it after.
    """
    src = _read()
    handler_start = src.find("def _upsert_job(")
    handler_end = src.find("\ndef _scan_board", handler_start)
    body = src[handler_start:handler_end]
    # Find the IntegrityError-survivor branch
    err_idx = body.find("except IntegrityError")
    assert err_idx > 0
    branch = body[err_idx:]
    assert '"_also_seen_on"' in branch, (
        "F316b regression: survivor-update branch no longer "
        "preserves the ``_also_seen_on`` tracking. "
        "Cross-platform sighting history is clobbered on every "
        "race-recovery."
    )
    # Must read BEFORE the reassign.
    capture_idx = branch.find("carried_also_seen")
    raw_assign_idx = branch.find("survivor.raw_json = raw_job")
    assert capture_idx > 0 and raw_assign_idx > capture_idx, (
        "F316b regression: ``carried_also_seen`` capture must "
        "happen BEFORE ``survivor.raw_json`` is reassigned, "
        "otherwise the read returns the new (empty-of-also-seen) "
        "raw_json instead of the prior value."
    )


def test_survivor_path_records_new_sighting():
    """The race-recovery path is itself a cross-platform sighting
    (whoever won the race inserted on a sibling platform; we
    just observed our intended platform). Stamp it onto
    ``_also_seen_on`` so the lineage tracking matches what the
    F88 cross-platform soft-match branch does.
    """
    src = _read()
    handler_start = src.find("def _upsert_job(")
    handler_end = src.find("\ndef _scan_board", handler_start)
    body = src[handler_start:handler_end]
    err_idx = body.find("except IntegrityError")
    branch = body[err_idx:]
    assert "board.platform != survivor.platform" in branch, (
        "F316b regression: survivor-update branch no longer "
        "guards on platform mismatch before recording the "
        "sighting. Either the platform-skip guard is gone "
        "(would record same-platform sightings, noise) or the "
        "tracking is gone entirely."
    )
    assert "board.platform" in branch and "external_id" in branch, (
        "F316b regression: sighting key (``platform:external_id``) "
        "no longer recorded onto _also_seen_on."
    )
