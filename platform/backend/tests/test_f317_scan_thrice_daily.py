"""F317 — scan_all_platforms runs thrice daily in BOTH modes.

Operator feedback: "lets run the scrape jobs thrice a day".
Pre-fix:
  - aggressive mode: ``crontab(minute="*/30")`` = 48/day
  - normal mode: ``crontab(minute=0, hour="8,20")`` = 2/day

The 48/day cadence was a contributing factor to the F316
duplicate-jobs problem — every cycle exercised the dedup-edge
cases (case/whitespace variants, missing title_normalized on
unclassified jobs, concurrent-worker races past the F88 lookup)
many more times per day. Operator's product judgment: 3 evenly-
spaced scans are sufficient (job postings don't change at
sub-hour granularity meaningfully).

F317 sets BOTH modes to ``crontab(minute=0, hour="0,8,16")`` —
midnight, 8am, 4pm UTC, evenly 8 hours apart. The SCAN_MODE flag
still differentiates the OTHER tasks (career-pages, discovery,
fingerprinting) but the headline "how often is jobs scrape"
answer is consistent across modes.
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
os.environ.setdefault("JWT_SECRET", "pytest-f317")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read() -> str:
    return (_BACKEND / "app" / "workers" / "celery_app.py").read_text()


def test_aggressive_mode_uses_thrice_daily():
    """The aggressive-mode beat_schedule must NOT use the
    pre-F317 ``minute="*/30"`` cadence and must use the
    explicit ``hour="0,8,16"`` thrice-daily form.
    """
    src = _read()
    # Find the aggressive branch.
    agg_start = src.find('SCAN_MODE == "aggressive"')
    assert agg_start > 0, (
        "F317 regression: aggressive SCAN_MODE branch missing"
    )
    agg_end = src.find("else:", agg_start)
    assert agg_end > agg_start
    agg_body = src[agg_start:agg_end]
    # Pre-fix anti-pattern that should NOT come back
    assert 'crontab(minute="*/30")' not in agg_body, (
        "F317 regression: aggressive scan reverted to every-30-min "
        "cadence. F316 dup-jobs surface widens again."
    )
    # The new cadence must appear inside the scan_all_platforms entry.
    scan_idx = agg_body.find('"scan_all_platforms"')
    assert scan_idx >= 0
    # Slice the scan_all_platforms task block (until the next ``}``).
    scan_block = agg_body[scan_idx:scan_idx + 400]
    assert 'hour="0,8,16"' in scan_block, (
        "F317 regression: aggressive scan_all_platforms cron no "
        "longer uses ``hour=\"0,8,16\"``. Operator-requested "
        "thrice-daily cadence is gone."
    )


def test_normal_mode_uses_thrice_daily():
    """Normal-mode beat_schedule also bumped to thrice daily so
    the SCAN_MODE flag doesn't change the headline 'how often
    are we scanning jobs' answer."""
    src = _read()
    agg_start = src.find('SCAN_MODE == "aggressive"')
    else_start = src.find("else:", agg_start)
    normal_body = src[else_start:]
    scan_idx = normal_body.find('"scan_all_platforms"')
    assert scan_idx >= 0
    scan_block = normal_body[scan_idx:scan_idx + 400]
    assert 'hour="0,8,16"' in scan_block, (
        "F317 regression: normal scan_all_platforms cron no "
        "longer uses ``hour=\"0,8,16\"``. Modes have drifted "
        "from the documented thrice-daily contract."
    )


def test_both_modes_use_same_scan_cadence():
    """Symmetry — both modes should agree on the scan-jobs
    cadence (the SCAN_MODE flag governs everything else like
    discovery/career-pages, but jobs scraping is operator-
    decided to be thrice daily either way).
    """
    src = _read()
    # Count ``hour="0,8,16"`` occurrences in scan_all_platforms
    # blocks. There's one for each mode.
    occurrences = src.count('"schedule": crontab(minute=0, hour="0,8,16")')
    assert occurrences >= 2, (
        f"F317 regression: only {occurrences} occurrence(s) of "
        f"the thrice-daily cron expression for "
        f"scan_all_platforms. Both aggressive AND normal modes "
        f"must use it."
    )
