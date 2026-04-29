"""F284 — /monitoring/backup concurrency lock + label sanitization
(closes F143).

F143 found three issues on ``POST /monitoring/backup``:
  (a) no concurrent-job guard — multiple admin clicks queue
      multiple Celery tasks that race on the
      ``BACKUP_ROOT/<ts>/`` directory under multi-worker Celery,
      corrupting manifests when timestamps collide;
  (b) ``label`` query param accepts arbitrary strings — control
      bytes, 5KB payloads, log-injection vectors via the
      manifest+access-log path;
  (c) no length cap.

F284 ships:
  * ``"backup"`` scope added to ``scan_lock`` TTL bucket map (10
    min — covers worst-case dump duration).
  * ``trigger_backup`` calls ``acquire_scan_lock("backup")`` and
    returns 409 if another backup is in-flight.
  * ``_sanitize_backup_label`` strips control bytes (0x00-0x1F,
    0x7F-0x9F) and trims; empty after strip falls back to
    ``"manual"``.
  * ``label: str = Query(..., max_length=64)`` Pydantic-validates
    the cap.
  * ``run_backup`` task releases the lock in its outermost
    cleanup so back-to-back backups are possible after one
    finishes.
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
os.environ.setdefault("JWT_SECRET", "pytest-f284")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_scan_lock_module_includes_backup_scope():
    """``"backup"`` must be in the TTL map so
    ``acquire_scan_lock("backup")`` doesn't fall back to the
    default 1800s. The 600s default is tuned to slightly exceed
    the worst-case dump duration on the documented prod scale.
    """
    src = (_BACKEND / "app" / "utils" / "scan_lock.py").read_text()
    assert '"backup":' in src, (
        "F284 regression: ``scan_lock`` module no longer declares "
        "a ``backup`` TTL bucket. ``acquire_scan_lock(\"backup\")`` "
        "would fall through to the default — still functional but "
        "the TTL would be wrong (default is 1800s for unknown "
        "prefixes; backup should be 600s)."
    )


def test_trigger_backup_handler_acquires_lock():
    """The handler must call ``acquire_scan_lock("backup")`` BEFORE
    queueing the Celery task. Without this, two concurrent admin
    triggers both queue tasks that race on
    ``BACKUP_ROOT/<ts>/``.
    """
    src = (_BACKEND / "app" / "api" / "v1" / "monitoring.py").read_text()
    handler_start = src.find("async def trigger_backup")
    assert handler_start >= 0
    handler_end = src.find("@router.", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    handler = src[handler_start:handler_end]
    assert "acquire_scan_lock(" in handler, (
        "F284 regression: ``trigger_backup`` no longer calls "
        "``acquire_scan_lock``. Concurrent triggers can race again."
    )
    assert '"backup"' in handler, (
        "F284 regression: ``trigger_backup`` no longer scopes the "
        "lock to ``\"backup\"``. A wrong scope means it doesn't "
        "actually gate other backup attempts."
    )
    assert "status_code=409" in handler, (
        "F284 regression: ``trigger_backup`` no longer returns 409 "
        "when the lock is held. The handler now silently queues a "
        "duplicate task on conflict."
    )


def test_trigger_backup_handler_caps_label_length():
    """The label must be Pydantic-capped. Without ``max_length``,
    a 5KB label flows into ``manifest.json`` + access logs.
    """
    src = (_BACKEND / "app" / "api" / "v1" / "monitoring.py").read_text()
    handler_start = src.find("async def trigger_backup")
    handler_end = src.find("@router.", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    handler = src[handler_start:handler_end]
    assert "max_length=" in handler, (
        "F284 regression: ``trigger_backup`` label no longer has a "
        "``max_length`` cap. 5KB labels can flow into manifest + logs."
    )
    assert "_BACKUP_LABEL_MAX_LEN" in handler or "max_length=64" in handler, (
        "F284 regression: backup label cap drifted from the "
        "documented 64-char ceiling."
    )


def test_label_sanitizer_strips_control_bytes():
    """The sanitizer must drop 0x00-0x1F + 0x7F-0x9F. Newlines
    in the label corrupt line-delimited JSON; null bytes corrupt
    the access log; CR/LF enable log-injection."""
    from app.api.v1.monitoring import _sanitize_backup_label

    # Newline injection
    assert "\n" not in _sanitize_backup_label("foo\nbar")
    # CR injection
    assert "\r" not in _sanitize_backup_label("foo\rbar")
    # NUL byte
    assert "\x00" not in _sanitize_backup_label("foo\x00bar")
    # TAB
    assert "\t" not in _sanitize_backup_label("foo\tbar")
    # 0x7F DEL
    assert "\x7f" not in _sanitize_backup_label("foo\x7fbar")


def test_label_sanitizer_falls_back_to_manual_when_empty():
    """Empty input (or all-control-byte input) falls back to
    ``"manual"`` so the task contract stays consistent.
    """
    from app.api.v1.monitoring import _sanitize_backup_label

    assert _sanitize_backup_label("") == "manual"
    assert _sanitize_backup_label(None) == "manual"  # type: ignore[arg-type]
    assert _sanitize_backup_label("   ") == "manual"
    assert _sanitize_backup_label("\n\r\t\x00") == "manual"


def test_label_sanitizer_passes_through_legit_input():
    """Normal labels round-trip unchanged."""
    from app.api.v1.monitoring import _sanitize_backup_label

    assert _sanitize_backup_label("manual") == "manual"
    assert _sanitize_backup_label("post-deploy v2.1") == "post-deploy v2.1"
    assert _sanitize_backup_label("schema-migration-2026-04-29") == "schema-migration-2026-04-29"


def test_run_backup_task_releases_lock():
    """The Celery task must call ``release_scan_lock("backup")``
    on completion (success, failure, or retry-raise) so the next
    scheduled or manual backup can acquire.
    """
    src = (_BACKEND / "app" / "workers" / "tasks" / "backup_task.py").read_text()
    assert "release_scan_lock" in src, (
        "F284 regression: ``run_backup`` task no longer releases "
        "the lock. Once acquired, no future backup can run until "
        "the TTL expires (10 min)."
    )
    assert '"backup"' in src or "'backup'" in src, (
        "F284 regression: ``run_backup`` releases the wrong scope. "
        "Must release the same scope it was acquired under."
    )
