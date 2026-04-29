"""F280 — bounded /export/* endpoints (closes F107 DoS surface).

Pre-fix, every export endpoint did
``await db.execute(query); rows = result.unique().scalars().all()``
with no LIMIT — full-table dumps materialised the entire result
set into Python RAM before streaming a single byte. F107 measured
~50 MB of joined-row objects per call against the 54k-row jobs
table; three concurrent callers could push the 1 GB backend
container into OOM territory. Compounding factor: ``_iter_csv``
buffered rows in memory before yielding, doubling the peak.

The fix is two-pronged:
  * Required-default ``limit`` Query parameter (default 5000,
    hard max 50000) applied via ``query.limit(limit)`` so every
    export is capped at the SQL layer.
  * ``limit`` echoed into the audit-log metadata so forensic
    analysis can distinguish "small filtered view" from
    "deliberate bulk pull".

These tests are source-level structural checks. The actual
runtime memory bound is verified in ad-hoc manual probes; we
keep regression coverage on the *shape* (limit param exists,
limit is applied, limit is audited) so a future refactor can't
silently re-introduce the unbounded surface.
"""
from __future__ import annotations

import os
import pathlib
import re

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f280")


_EXPORT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "api" / "v1" / "export.py"
)


def _read() -> str:
    return _EXPORT_PATH.read_text()


def test_export_module_declares_limit_constants():
    """Both ``EXPORT_DEFAULT_LIMIT`` and ``EXPORT_MAX_LIMIT`` must
    be module-level constants so every endpoint shares the same
    ceiling and ops can tune them without grepping individual
    handlers.
    """
    src = _read()
    assert "EXPORT_DEFAULT_LIMIT = " in src, (
        "F280 regression: ``EXPORT_DEFAULT_LIMIT`` constant removed. "
        "Without a shared constant, future handlers may drift on "
        "the default."
    )
    assert "EXPORT_MAX_LIMIT = " in src, (
        "F280 regression: ``EXPORT_MAX_LIMIT`` constant removed. "
        "Without a shared hard ceiling, /export/* endpoints can "
        "drift back toward unbounded."
    )


def test_export_jobs_handler_has_bounded_limit_param():
    """``/export/jobs`` must accept ``limit: int = Query(...)``
    with both ``ge=1`` and ``le=EXPORT_MAX_LIMIT``. The bounded
    ceiling protects against ``?limit=99999999`` foot-guns.
    """
    src = _read()
    handler = _slice_handler(src, "export_jobs")
    assert "limit: int = Query(" in handler, (
        "F280 regression: /export/jobs no longer declares a "
        "``limit: int = Query(...)`` parameter. The DoS bound is gone."
    )
    assert "EXPORT_DEFAULT_LIMIT" in handler, (
        "F280 regression: /export/jobs no longer references the "
        "shared default-limit constant."
    )
    assert "EXPORT_MAX_LIMIT" in handler, (
        "F280 regression: /export/jobs no longer references the "
        "shared max-limit ceiling. ``?limit=999999999`` would now "
        "pass and the handler could OOM the container again."
    )


def test_export_pipeline_handler_has_bounded_limit_param():
    src = _read()
    handler = _slice_handler(src, "export_pipeline")
    assert "limit: int = Query(" in handler
    assert "EXPORT_MAX_LIMIT" in handler


def test_export_contacts_handler_has_bounded_limit_param():
    """The contacts export is the LARGEST and MOST SENSITIVE of
    the three (PII + outreach metadata). The limit cap is
    load-bearing here — both for memory AND exfiltration bounds.
    """
    src = _read()
    handler = _slice_handler(src, "export_contacts")
    assert "limit: int = Query(" in handler, (
        "F280 regression: /export/contacts (the largest + most "
        "sensitive export) no longer enforces a row limit."
    )
    assert "EXPORT_MAX_LIMIT" in handler


def test_each_export_handler_applies_limit_to_query():
    """The Query param has to actually thread into the SQL —
    declaring ``limit`` in the signature without applying it via
    ``.limit(limit)`` would be the worst-of-both-worlds: the API
    advertises a cap but the DB still returns the full set.
    """
    src = _read()
    for handler_name in ("export_jobs", "export_pipeline", "export_contacts"):
        handler = _slice_handler(src, handler_name)
        assert ".limit(limit)" in handler, (
            f"F280 regression: {handler_name} accepts a ``limit`` "
            f"param but doesn't apply it to the SQL query — the "
            f"endpoint advertises a cap that the DB ignores."
        )


def test_each_export_audit_log_records_limit():
    """Forensic analysis (who pulled the big bulk dump and when?)
    needs the operator-chosen ``limit`` in the audit-log metadata.
    Without it, all exports look identical in the audit table.
    """
    src = _read()
    for handler_name in ("export_jobs", "export_pipeline", "export_contacts"):
        handler = _slice_handler(src, handler_name)
        # Each handler must (a) call log_action and (b) include the
        # ``limit`` field in the metadata dict. Looking for the
        # ``"limit": limit`` literal — anywhere in the handler is
        # fine since the handler body is narrowly sliced already.
        assert "log_action(" in handler, (
            f"F280 regression: {handler_name} no longer calls "
            f"``log_action(...)``. F61 audit trail is gone."
        )
        assert '"limit": limit' in handler, (
            f"F280 regression: {handler_name} audit log metadata "
            f"no longer records the ``limit`` value. Forensic "
            f"analysis can't distinguish bulk pulls from filtered "
            f"views."
        )


def _slice_handler(src: str, handler_name: str) -> str:
    """Return the function body for ``async def handler_name(...)``.
    Slices from the ``async def`` line to the next ``@router.`` or
    EOF — coarse but resilient to indentation changes.
    """
    start = src.find(f"async def {handler_name}(")
    assert start >= 0, (
        f"F280 regression: ``async def {handler_name}`` no longer "
        f"present in export.py. File structure changed."
    )
    next_router = src.find("@router.", start + 1)
    return src[start:next_router] if next_router > 0 else src[start:]
