"""F296 — Workable fetcher requests inline descriptions (closes F121, Workable side).

F121 found the Workable + SmartRecruiters fetchers were calling
listing endpoints whose payloads didn't include description fields
at all. Every Workable job in the DB at F121 verification time had
empty ``JobDescription`` rows + ``/jobs/{id}/description`` returned
0 chars.

Workable supports inline description via the
``?details=true&full_description=true`` widget API params. F296
adds those params to the fetcher URL. ``extract_description`` at
``utils/job_description.py:39`` already maps the ``full_description``
/ ``description`` keys, so the scan-time write to ``JobDescription``
populates automatically once the response carries the field. The
F291 ``/jobs/{id}/description`` handler also picks up the new
content via the same shared helper.

SmartRecruiters side of F121 needs a different fix (per-posting
detail endpoint) and is deferred.
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
os.environ.setdefault("JWT_SECRET", "pytest-f296")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_workable_api_url_requests_full_description():
    """The fetcher URL must carry ``details=true`` + ``full_description=true``
    query params so the widget endpoint inlines the description text.
    """
    from app.fetchers.workable import API_URL

    assert "details=true" in API_URL, (
        "F296 regression: ``details=true`` query param removed from "
        "``API_URL``. Workable returns listing-only payloads without "
        "this param, so descriptions are empty again."
    )
    assert "full_description=true" in API_URL, (
        "F296 regression: ``full_description=true`` query param "
        "removed from ``API_URL``. Description text won't be "
        "inlined."
    )


def test_extract_description_already_maps_workable_keys():
    """The shared helper has Workable's keys in ``_HTML_KEYS_BY_PLATFORM``
    so the new inline description threads through to the DB row
    without further plumbing changes. This test guards against a
    future contributor renaming the keys without realising F296 +
    F291 depend on them.
    """
    from app.utils.job_description import _HTML_KEYS_BY_PLATFORM

    workable_keys = _HTML_KEYS_BY_PLATFORM.get("workable", ())
    assert "full_description" in workable_keys, (
        "F296 regression: ``full_description`` no longer in the "
        "Workable key map. Inline description from F296's URL "
        "params won't be extracted."
    )
    assert "description" in workable_keys, (
        "F296 regression: ``description`` fallback no longer in "
        "the Workable key map."
    )
