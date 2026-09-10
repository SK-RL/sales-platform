"""Every kwarg ``run_backup`` passes to ``ScanLog`` must be a real column.

The nightly backup had never produced a single file. Two stacked causes,
the second of which hid the first:

  1. ``pg_dump``/``psql`` were absent from the backend image, so step 1
     (``_row_counts``) failed immediately. Fixed in the Dockerfile by
     installing PGDG ``postgresql-client-16`` — pinned to the server's
     major version, because a 17 client writes custom-format archives
     that ``pg_restore`` 16 cannot read.
  2. The bookkeeping ``ScanLog(...)`` at the end of ``run_backup`` passed
     five kwargs naming columns that do not exist — ``board_slug``,
     ``jobs_new``, ``jobs_updated``, ``status``, ``finished_at``.
     SQLAlchemy's declarative constructor raises ``TypeError`` on the
     first of those, and the call sits outside any ``try``, *after* the
     except block has already swallowed the real error. So the task died
     at that line on every run and recorded nothing: zero rows in
     ``scan_logs`` where ``platform = 'backup'``, for months, while no
     backup existed.

The existing backup tests (``test_f284_backup_lock_label_sanitize``)
assert against the *source text* of the module, so neither noticed that
the field names were wrong. This one checks the kwargs against the
model's actual columns, closing the class rather than the instance —
same spirit as the F356 "every beat task is registered" invariant.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-backup-scanlog")

_BACKEND = Path(__file__).resolve().parents[1]
_TASK = _BACKEND / "app" / "workers" / "tasks" / "backup_task.py"


def _scanlog_kwargs_in_backup_task() -> set[str]:
    """Kwarg names passed to the ``ScanLog(...)`` call in backup_task."""
    src = _TASK.read_text()
    match = re.search(r"ScanLog\(\s*(.*?)\n\s*\)", src, re.DOTALL)
    assert match, "could not locate the ScanLog(...) call in backup_task.py"
    # `(?!=)` keeps comparisons out of the result — `status == "ok"` inside
    # a conditional kwarg value is not itself a kwarg name.
    return set(re.findall(r"(\w+)\s*=(?!=)", match.group(1)))


def test_backup_scanlog_kwargs_are_real_columns():
    from app.models.scan import ScanLog

    columns = set(ScanLog.__table__.columns.keys())
    kwargs = _scanlog_kwargs_in_backup_task()

    assert kwargs, "no kwargs parsed from the ScanLog(...) call"
    unknown = kwargs - columns
    assert not unknown, (
        f"run_backup passes ScanLog kwargs that are not columns: "
        f"{sorted(unknown)}. Valid columns: {sorted(columns)}. "
        "SQLAlchemy raises TypeError on these, and the call is outside "
        "any try/except — so the task dies without recording anything."
    )


def test_backup_scanlog_row_constructs():
    """The exact field set must actually build a ScanLog instance."""
    from datetime import datetime, timezone

    from app.models.scan import ScanLog

    now = datetime.now(timezone.utc)
    row = ScanLog(
        source="backup/20260910_063450",
        platform="backup",
        started_at=now,
        completed_at=now,
        jobs_found=0,
        new_jobs=0,
        updated_jobs=0,
        errors=1,
        error_message="pg_dump failed: boom",
        duration_ms=1234,
    )
    assert row.platform == "backup"
    assert row.errors == 1


def test_error_message_is_never_none():
    """``error_message`` is a NOT NULL column with a '' default.

    The pre-fix code passed ``error_msg if error_msg else None``, which
    would violate the constraint on the success path even once the field
    names were right.
    """
    src = _TASK.read_text()
    assert "error_message=error_msg if error_msg else None" not in src, (
        "error_message must not be set to None — the column is NOT NULL; "
        "pass the empty string (error_msg is '' on success)."
    )
