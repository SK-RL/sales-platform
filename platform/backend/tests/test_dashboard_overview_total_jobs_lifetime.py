"""Dashboard "Total Jobs" tile must surface lifetime cardinality.

User feedback 2026-05-24 (in-app):

  "In home page the total jobs number seems incorrect"

Root cause: F214 re-scoped ``/analytics/overview.total_jobs`` from
"lifetime count of jobs" to "jobs first_seen_at within the last
``days`` window" (default 30), in preparation for a future "Last
N days" switcher. The DashboardPage "Total Jobs" tile, however,
renders alongside lifetime cardinality tiles (Companies, Pipeline
Active) and users read it as "how many jobs exist" — the same
number /monitoring's ``data_counts.jobs`` reports.

Fix: ``/analytics/overview`` now also returns
``total_jobs_lifetime`` (un-windowed ``SELECT count(*) FROM
jobs``), and the dashboard tile reads from that field. F214's
windowed ``total_jobs`` stays intact so the future switcher can
opt back in without another schema change.

These tests are STATIC — they read the source files and assert
the wiring. That gives the regression guard without depending on
a live DB / docker stack, matching the pattern used by F340/F342
and the rest of the F-test suite.
"""
from __future__ import annotations

import pathlib


_PLATFORM = pathlib.Path(__file__).resolve().parents[2]
_BACKEND = _PLATFORM / "backend"
_FRONTEND = _PLATFORM / "frontend"


def _read(p: pathlib.Path) -> str:
    return p.read_text()


def test_overview_endpoint_exposes_lifetime_field():
    """``/analytics/overview`` must include ``total_jobs_lifetime`` in
    its response. A future refactor that drops the field (or renames
    it) would silently revert the dashboard tile to the windowed
    value and re-introduce the original complaint."""
    src = _read(_BACKEND / "app" / "api" / "v1" / "analytics.py")
    # Field present in the returned dict literal.
    assert '"total_jobs_lifetime": total_jobs_lifetime' in src, (
        "/analytics/overview must return total_jobs_lifetime"
    )
    # And it must be computed from an un-windowed query — the
    # whole point of the fix. Anchor on the assignment statement
    # itself so the test ignores explanatory comments above/below.
    assign_idx = src.find("total_jobs_lifetime = ")
    assert assign_idx > 0, "expected `total_jobs_lifetime = …` assignment"
    # Walk back to the first `lifetime_q = …` definition that feeds
    # the assignment, then capture through the assignment so we see
    # both the base query and any conditional `.where(...)` chained
    # onto it.
    query_start = src.find("lifetime_q = ", 0, assign_idx)
    assert query_start > 0, "expected `lifetime_q = …` query definition"
    query_block = src[query_start: assign_idx + 200]
    assert "select(func.count(Job.id))" in query_block, (
        "lifetime count must be an un-windowed COUNT(jobs.id)"
    )
    # Crucially the lifetime query must NOT filter by first_seen_at
    # (that's the F214 windowed behavior the fix is correcting).
    # Allow `cluster_filter` to apply (cardinality of jobs *in this
    # cluster* is still cardinality), but reject any time-window
    # predicate on the lifetime query.
    assert "first_seen_at" not in query_block, (
        "lifetime count must not be windowed by first_seen_at"
    )


def test_dashboard_total_jobs_tile_reads_lifetime_field():
    """``DashboardPage`` "Total Jobs" StatCard must consume
    ``overview.total_jobs_lifetime``. A future edit that flips the
    tile back to ``overview.total_jobs`` would re-introduce the
    "Total Jobs shows last-30-days only" bug."""
    src = _read(_FRONTEND / "src" / "pages" / "DashboardPage.tsx")
    # Locate the Total Jobs StatCard. Anchor on the label to stay
    # robust against StatCard prop reordering or formatting churn.
    label_idx = src.find('label="Total Jobs"')
    assert label_idx > 0, "Total Jobs StatCard not found in DashboardPage"
    # Look at the surrounding ~400 chars (prev label + value + next).
    window = src[max(0, label_idx - 200): label_idx + 400]
    assert "total_jobs_lifetime" in window, (
        "Total Jobs tile must read overview.total_jobs_lifetime"
    )
    # And it must NOT be reading the windowed `total_jobs` field —
    # the very bug being fixed. Match on `?.total_jobs` with NO
    # `_lifetime` suffix so we don't false-positive on the lifetime
    # field's substring.
    import re
    bad = re.search(r"overview\?\.total_jobs(?!_lifetime)", window)
    assert bad is None, (
        "Total Jobs tile must not read overview.total_jobs "
        "(the F214 windowed value) — it reintroduces the bug"
    )


def test_analytics_overview_type_declares_lifetime_field():
    """Frontend ``AnalyticsOverview`` type must declare
    ``total_jobs_lifetime``. Without the type entry, the dashboard
    edit above would fail strict TS compilation — but only the
    type stops a future caller from re-introducing
    ``overview.total_jobs`` and getting a clean type-check."""
    src = _read(_FRONTEND / "src" / "lib" / "types.ts")
    iface_idx = src.find("export interface AnalyticsOverview")
    assert iface_idx > 0, "AnalyticsOverview interface missing"
    # Look at the interface body (up to the closing brace).
    body = src[iface_idx: src.find("}", iface_idx) + 1]
    assert "total_jobs_lifetime: number" in body, (
        "AnalyticsOverview must declare total_jobs_lifetime: number"
    )
