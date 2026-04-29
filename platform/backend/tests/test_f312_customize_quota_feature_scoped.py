"""F312 — /resume/{id}/customize no-key fallback feature-scoped quota count.

Pre-fix the no-API-key early-return branch counted ALL
``AICustomizationLog`` rows for the user today regardless of
``feature``. Result: ``usage.used_today`` summed customize +
cover_letter + interview_prep calls and returned a number larger
than the per-feature ``ai_daily_limit_per_user`` cap. That's
inconsistent with the ``check_ai_quota`` path used elsewhere in
the same handler — same user, same call, two different "used
today" numbers depending on whether the API key happens to be
configured at request time.

F312 adds the ``AICustomizationLog.feature == AI_FEATURE_CUSTOMIZE``
filter so the no-key path matches the with-key path.
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
os.environ.setdefault("JWT_SECRET", "pytest-f312")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "resume.py").read_text()


def test_no_key_fallback_filters_by_feature():
    """The no-API-key early-return branch must filter by
    ``feature == AI_FEATURE_CUSTOMIZE``. Without it, the
    ``used_today`` count is cross-feature and contradicts the
    with-key path's per-feature count.
    """
    src = _read()
    # Find the no-key branch in customize_resume_for_job.
    no_key_idx = src.find("anthropic_api_key.get_secret_value()")
    assert no_key_idx > 0, (
        "F312 regression: no-key early-return branch removed."
    )
    # Slice ~40 lines after the branch start to grab the count
    # query.
    branch = src[no_key_idx:no_key_idx + 2000]
    assert "AICustomizationLog.feature ==" in branch, (
        "F312 regression: no-key branch's count query no longer "
        "filters by feature. The cross-feature count returns the "
        "wrong used_today number again."
    )
    assert "AI_FEATURE_CUSTOMIZE" in branch, (
        "F312 regression: feature constant ``AI_FEATURE_CUSTOMIZE`` "
        "no longer used. Hardcoded string would drift if the "
        "constant is renamed."
    )


def test_existing_success_filter_kept():
    """F312 ADDED a feature filter — the pre-existing success
    filter (F170/F203) must still be in place.
    """
    src = _read()
    no_key_idx = src.find("anthropic_api_key.get_secret_value()")
    branch = src[no_key_idx:no_key_idx + 2000]
    assert "AICustomizationLog.success == True" in branch, (
        "F312 regression: ``success == True`` filter dropped. "
        "Failed calls would be re-counted against the user's quota "
        "(F170/F203 reopens)."
    )
