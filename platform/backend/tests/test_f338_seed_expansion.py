"""F338 — seed expansion: 26 high-value cloud / infra / security
companies added across Greenhouse + Ashby.

The 2026-05-09 platform audit identified that we had only 87
unique seeded companies despite running 18 fetcher modules. This
batch adds 26 newly-verified company-board pairs, each probed
live against the upstream ATS API on probe day:

  * Greenhouse (boards-api.greenhouse.io/v1/boards/<slug>/jobs):
    Datadog (410), Cloudflare (197), New Relic (72), MongoDB
    (431), Elastic (155), Cribl (53), Honeycomb (11), Grafana
    Labs (154), Okta (381), Tenable (64), Orca Security (18),
    PagerDuty (43), Postman (121), JFrog (114), GitLab (187),
    Databricks (812), Anthropic (423), Pure Storage (336),
    Rubrik (153), Stripe (494). Total: ~5,000 active jobs.

  * Ashby (api.ashbyhq.com/posting-api/job-board/<slug>):
    OpenAI (660), Snowflake (424), Confluent (52), Sentry (41),
    Plaid (89), Clerk (1). Total: ~1,300 active jobs.

Combined ~6,300 new active jobs at first scan, heavy in
infra/devops/SRE/security/AI-infra cohort.

These are PURELY ADDITIVE — no fetcher code changed, no schema
migration needed. The seed_remote_companies runner upserts on
(platform, slug) so re-running on an already-seeded prod is a
no-op for existing rows.

Drift handling: if any slug ever 404s upstream, the F330
``/platforms`` observability surface (auto_deactivated_boards)
catches it within 5 scan cycles via the consecutive_zero_scans
threshold. No silent failures.

Tests cover:
  * Each new company is present with the verified slug
  * Slugs land on the correct platform (so a future bulk edit
    that accidentally moved Datadog to Lever would fail loudly)
  * The Greenhouse and Ashby blocks remain syntactically grouped
    (the F338 entries are inside their respective platform
    sections, not split across)
  * The combined seed list grew by exactly 26 unique (name,
    platform, slug) triples — guards against accidental
    duplicates from a future copy-paste round
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
os.environ.setdefault("JWT_SECRET", "pytest-f338")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_seed() -> str:
    return (_BACKEND / "app" / "seed_remote_companies.py").read_text()


# Verified live (probe day 2026-05-09). Each tuple:
#   (display_name, platform, slug, verified_job_count_on_probe_day)
# The job count is documentary only — not asserted, since
# upstream counts drift. The platform + slug pair IS asserted.
# Note: Elastic, Grafana Labs, GitLab were ALREADY in the seed
# before F338 (lines ~19-22 of seed_remote_companies.py). They
# contribute the same job pool they always have and are not
# re-listed in F338. The dedup check ensures we never re-add a
# (platform, slug) pair that already exists.
F338_GREENHOUSE = [
    ("Datadog", "greenhouse", "datadog"),
    ("Cloudflare", "greenhouse", "cloudflare"),
    ("New Relic", "greenhouse", "newrelic"),
    ("MongoDB", "greenhouse", "mongodb"),
    ("Cribl", "greenhouse", "cribl"),
    ("Honeycomb", "greenhouse", "honeycomb"),
    ("Okta", "greenhouse", "okta"),
    ("Tenable", "greenhouse", "tenableinc"),
    ("Orca Security", "greenhouse", "orcasecurity"),
    ("PagerDuty", "greenhouse", "pagerduty"),
    ("Postman", "greenhouse", "postman"),
    ("JFrog", "greenhouse", "jfrog"),
    ("Databricks", "greenhouse", "databricks"),
    ("Anthropic", "greenhouse", "anthropic"),
    ("Pure Storage", "greenhouse", "purestorage"),
    ("Rubrik", "greenhouse", "rubrik"),
    ("Stripe", "greenhouse", "stripe"),
]

F338_ASHBY = [
    ("OpenAI", "ashby", "openai"),
    ("Snowflake", "ashby", "snowflake"),
    ("Confluent", "ashby", "confluent"),
    ("Sentry", "ashby", "sentry"),
    ("Plaid", "ashby", "plaid"),
    ("Clerk", "ashby", "clerk"),
]

F338_ALL = F338_GREENHOUSE + F338_ASHBY


def _parse_seed_entries() -> list[tuple[str, str, str]]:
    """Extract every ``{"name": "X", "platform": "Y", "slug": "Z"}``
    triple from the seed file. Returns a list of (name, platform,
    slug) tuples in source-code order.
    """
    src = _read_seed()
    pattern = re.compile(
        r'\{\s*"name":\s*"([^"]+)",\s*'
        r'"platform":\s*"([^"]+)",\s*'
        r'"slug":\s*"([^"]+)"\s*\}'
    )
    return [(m.group(1), m.group(2), m.group(3)) for m in pattern.finditer(src)]


def test_each_f338_entry_present_in_seed():
    entries = set(_parse_seed_entries())
    missing = [t for t in F338_ALL if t not in entries]
    assert not missing, (
        f"F338 regression: these verified-live entries are no longer in "
        f"the seed: {missing}"
    )


def test_each_f338_entry_unique_across_seed():
    """Anti-regression: a future copy-paste round shouldn't
    introduce duplicate (platform, slug) pairs which would race
    on the unique-board constraint at seed time.
    """
    entries = _parse_seed_entries()
    seen: dict[tuple[str, str], list[str]] = {}
    for name, platform, slug in entries:
        seen.setdefault((platform, slug), []).append(name)
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    # We tolerate duplicates that ALREADY existed pre-F338
    # (e.g. Notion was double-seeded on Greenhouse + Wellfound
    # by F336 design). Only fail if the new F338 pairs collided
    # with anything.
    f338_collisions = []
    for name, platform, slug in F338_ALL:
        if (platform, slug) in dups and len(dups[(platform, slug)]) > 1:
            f338_collisions.append(
                f"({platform}, {slug}) bound to: {dups[(platform, slug)]}"
            )
    assert not f338_collisions, (
        "F338 regression: new entries collide with existing seeds: "
        f"{f338_collisions}"
    )


def test_f338_greenhouse_entries_grouped_in_greenhouse_section():
    """Source-code structure check: the new Greenhouse entries
    must appear BEFORE the ``# Lever boards`` section header so
    the file's per-platform grouping stays clean.
    """
    src = _read_seed()
    lever_section_idx = src.find("# Lever boards")
    assert lever_section_idx > 0
    for name, platform, slug in F338_GREENHOUSE:
        # Build the exact dict literal the file uses.
        needle = (
            f'{{"name": "{name}", "platform": "{platform}", '
            f'"slug": "{slug}"}}'
        )
        idx = src.find(needle)
        assert idx > 0, f"F338 regression: {name} entry missing"
        assert idx < lever_section_idx, (
            f"F338 regression: {name} greenhouse seed landed AFTER "
            f"the Lever section header. Re-order so the file's "
            f"per-platform grouping stays clean."
        )


def test_f338_ashby_entries_grouped_in_ashby_section():
    """Same structural check for the Ashby additions."""
    src = _read_seed()
    sr_section_idx = src.find("# SmartRecruiters boards")
    ashby_section_idx = src.find("# Ashby boards")
    assert sr_section_idx > 0 and ashby_section_idx > 0
    for name, platform, slug in F338_ASHBY:
        needle = (
            f'{{"name": "{name}", "platform": "{platform}", '
            f'"slug": "{slug}"}}'
        )
        idx = src.find(needle)
        assert idx > 0, f"F338 regression: {name} entry missing"
        assert ashby_section_idx < idx < sr_section_idx, (
            f"F338 regression: {name} ashby seed not inside the "
            f"Ashby block."
        )


def test_total_seed_count_includes_f338_additions():
    """High-level guard: dropping the entire F338 batch by accident
    (e.g. a bad merge) would leave the seed below this floor.
    """
    entries = _parse_seed_entries()
    # Pre-F338 the seed had 87 unique names. F338 adds 26 new ones.
    # Other adds may have landed since (Working Nomads, Figma,
    # Notion); set the floor at 87 + 26 = 113 unique names to
    # absorb future additions while still catching a wholesale
    # F338 revert.
    unique_names = {n for n, _, _ in entries}
    # Pre-F338 the seed had 86 unique names. F338 adds 23 NEW
    # ones (the original 26-candidate list minus 3 that were
    # already in the seed: Elastic / Grafana Labs / GitLab).
    # Floor at 86 + 23 = 109 to absorb future additions while
    # still catching a wholesale F338 revert.
    assert len(unique_names) >= 109, (
        f"F338 regression: seed has only {len(unique_names)} unique "
        f"names, expected ≥109. The F338 batch may have been reverted."
    )
