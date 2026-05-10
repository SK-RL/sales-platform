"""F336 — route Figma + Notion through Greenhouse instead of Wellfound.

The 2026-05-09 platform audit (and earlier F116 🔴) flagged that
the Wellfound fetcher produces 0 jobs in prod across all 10
seeded boards because Wellfound now sits behind DataDome bot-
detection that blocks datacenter IP ranges (see
``app/fetchers/wellfound.py`` docstring's "Honest expectations"
section). The fetcher itself is correctly implemented but the
upstream is unreachable from the Oracle ARM VM without a
residential proxy / managed bypass service.

Of the 10 Wellfound-seeded companies, 8 are ALREADY double-
seeded on their actual ATS (Vercel/Supabase/dbt Labs/Snyk on
Greenhouse, Zapier on Lever, Tailscale/Linear on Ashby) so they
get full job coverage today via the working ATS path. Only
Figma + Notion were Wellfound-only — those two went unscored.

F336 adds Greenhouse seeds for both:

  * Figma → ``boards.greenhouse.io/figma``
  * Notion → ``boards.greenhouse.io/notion``

The dead Wellfound rows are LEFT IN PLACE and will auto-
deactivate after 5 consecutive zero scans via the existing
``CompanyATSBoard.consecutive_zero_scans`` mechanism (Finding 7
auto-deactivation + F330 ``/platforms`` observability surface).

Concrete prod uplift expected: Figma + Notion typically have
30-80 active engineering postings between them at any given
time, with strong infra/devops/SRE representation. Most are
``global_remote`` or ``usa_only``. Adding them as Greenhouse
boards routes those into the F332-fixed JD pipeline and the
F333-tightened relevance scorer.
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
os.environ.setdefault("JWT_SECRET", "pytest-f336")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_seed() -> str:
    return (_BACKEND / "app" / "seed_remote_companies.py").read_text()


def test_figma_added_on_greenhouse():
    src = _read_seed()
    assert (
        '"name": "Figma", "platform": "greenhouse"' in src
    ), (
        "F336 regression: Figma greenhouse seed removed. "
        "Figma's only seeded board is now Wellfound, which "
        "produces 0 jobs due to DataDome blocking."
    )


def test_notion_added_on_greenhouse():
    src = _read_seed()
    assert (
        '"name": "Notion", "platform": "greenhouse"' in src
    ), (
        "F336 regression: Notion greenhouse seed removed. "
        "Notion's only seeded board is now Wellfound, which "
        "produces 0 jobs due to DataDome blocking."
    )


def test_figma_greenhouse_slug_unchanged():
    """The Greenhouse slug ``figma`` must match
    ``boards.greenhouse.io/figma``. A future renamed slug would
    silently 404 the per-scan fetch.
    """
    src = _read_seed()
    assert '"name": "Figma", "platform": "greenhouse", "slug": "figma"' in src


def test_notion_greenhouse_slug_unchanged():
    src = _read_seed()
    assert '"name": "Notion", "platform": "greenhouse", "slug": "notion"' in src


def test_existing_wellfound_seeds_preserved_for_audit_trail():
    """We deliberately DON'T delete the Wellfound rows — they'll
    auto-deactivate via the consecutive_zero_scans mechanism and
    the F330 platform observability surface will display them as
    auto-deactivated. Removing them from the seed would lose
    audit-trail visibility ("why did Figma boards get added on
    two platforms?") that the inline F336 comment provides.
    """
    src = _read_seed()
    # The Wellfound entries should still be present in the file.
    assert '"platform": "wellfound"' in src, (
        "F336 regression: Wellfound seed entries removed. "
        "Auto-deactivation audit trail is lost; reviewers won't "
        "see why those boards exist."
    )
    # And specifically the Figma/Notion ones.
    assert '"name": "Figma", "platform": "wellfound"' in src
    assert '"name": "Notion", "platform": "wellfound"' in src


def test_greenhouse_seed_added_above_lever_section():
    """Anti-regression: the F336 entries must land in the
    Greenhouse section of the seed list so the tooling that
    groups by platform doesn't produce a split second Greenhouse
    block. Asserts the Figma greenhouse line appears BEFORE the
    "# Lever boards" section header.
    """
    src = _read_seed()
    figma_idx = src.find('"name": "Figma", "platform": "greenhouse"')
    lever_section_idx = src.find("# Lever boards")
    assert figma_idx > 0
    assert lever_section_idx > 0
    assert figma_idx < lever_section_idx, (
        "F336 regression: Figma greenhouse seed landed AFTER the "
        "Lever section header. Re-order so the seed list groups "
        "cleanly by platform."
    )
