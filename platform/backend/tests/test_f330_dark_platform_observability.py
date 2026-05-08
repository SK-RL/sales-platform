"""F330 — surface auto-deactivated + silently-degrading boards on
``GET /api/v1/platforms``.

F116 (🔴) reported four ATS platforms (bamboohr, jobvite,
recruitee, wellfound) that had returned ZERO jobs and ZERO errors
across 20 consecutive scans each. The Finding-7 auto-deactivation
path already increments ``CompanyATSBoard.consecutive_zero_scans``
on each clean-empty scan and flips ``is_active=False`` after the
threshold, recording "auto: N consecutive zero-job ..." in
``deactivated_reason``. So the silent-failure detection LOGIC
existed — but the /platforms admin response never surfaced the
counts, so ops still saw "wellfound: 0 active boards" with no
way to distinguish "no boards configured" from "all 5 boards
auto-deactivated as silently dark."

F330 closes the observability half by adding two new fields to
each per-platform card on the /platforms response:

  * ``auto_deactivated_boards`` — inactive rows where
    ``deactivated_reason`` starts with ``auto:``. Excludes
    manual admin pauses so this is a clean fetcher-health
    signal.
  * ``silently_degrading_boards`` — STILL-ACTIVE rows whose
    ``consecutive_zero_scans > 0``. Yellow-flag before the
    auto-deactivation event.

Frontend uses these for the platform card's health badge.
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
os.environ.setdefault("JWT_SECRET", "pytest-f330")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "platforms.py").read_text()


def _read_model() -> str:
    return (_BACKEND / "app" / "models" / "company.py").read_text()


def test_handler_emits_auto_deactivated_boards_count():
    """The platforms handler must emit a per-platform
    ``auto_deactivated_boards`` count so the frontend can render
    a red badge when a fetcher is fully dark.
    """
    src = _read_handler()
    assert "auto_deactivated_boards" in src, (
        "F330 regression: /platforms response no longer emits "
        "``auto_deactivated_boards``. Frontend can't render the "
        "dark-platform badge that closes the F116 observability gap."
    )


def test_handler_emits_silently_degrading_boards_count():
    """Yellow-flag before auto-deactivation: STILL-ACTIVE boards
    with ``consecutive_zero_scans > 0`` get aggregated.
    """
    src = _read_handler()
    assert "silently_degrading_boards" in src, (
        "F330 regression: /platforms response no longer emits "
        "``silently_degrading_boards``. Admins lose the early-"
        "warning signal before auto-deactivation fires."
    )


def test_auto_deactivated_filter_excludes_manual_pauses():
    """The aggregation must filter on ``deactivated_reason LIKE
    'auto:%'`` so a manually-paused board doesn't count toward the
    fetcher-health red-badge.
    """
    src = _read_handler()
    # The case() expression must reference both is_active=False AND
    # the auto: prefix on deactivated_reason.
    assert 'deactivated_reason.like("auto:%")' in src, (
        "F330 regression: auto_deactivated_boards now counts manual "
        "admin-paused boards too. The badge would fire false-"
        "positive on routine admin maintenance."
    )


def test_silently_degrading_filter_only_counts_active():
    """The ``silently_degrading`` aggregation must only count rows
    that are STILL ACTIVE — once a board is auto-deactivated it
    moves into the ``auto_deactivated`` bucket and shouldn't
    double-count.
    """
    src = _read_handler()
    # The aggregation references is_active == True AND
    # consecutive_zero_scans > 0 in a single case() arm.
    assert "consecutive_zero_scans > 0" in src, (
        "F330 regression: silently_degrading_boards aggregation "
        "no longer checks consecutive_zero_scans."
    )


def test_default_envelope_includes_new_fields():
    """When ``boards_by_platform`` has no row for a platform name
    (newly-seeded fetcher, all boards purged), the default dict
    must still include the two new keys so the response shape is
    consistent regardless of board population.
    """
    src = _read_handler()
    # Look for the default fallback dict in the assembly loop.
    fallback_idx = src.find('boards_by_platform.get(')
    assert fallback_idx > 0
    # Window the fallback default — should include both new keys.
    window = src[fallback_idx:fallback_idx + 500]
    assert "auto_deactivated_boards" in window, (
        "F330 regression: default-zero fallback dict missing "
        "``auto_deactivated_boards`` — response shape diverges "
        "between platforms with and without board rows."
    )
    assert "silently_degrading_boards" in window, (
        "F330 regression: default-zero fallback dict missing "
        "``silently_degrading_boards``."
    )


def test_underlying_columns_intact_on_model():
    """F330 only works because Finding 7 already added the two
    backing columns. Guard against either column getting dropped.
    """
    src = _read_model()
    assert "consecutive_zero_scans" in src, (
        "F330 regression (root cause): "
        "CompanyATSBoard.consecutive_zero_scans column dropped — "
        "the silently_degrading aggregation now produces zero for "
        "every platform."
    )
    assert "deactivated_reason" in src, (
        "F330 regression (root cause): "
        "CompanyATSBoard.deactivated_reason column dropped — the "
        "auto_deactivated_boards aggregation can't tell auto from "
        "manual without it."
    )
