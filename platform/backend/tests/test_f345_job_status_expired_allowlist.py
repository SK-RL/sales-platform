"""F345 — `expired` was missing from the job-status CHECK allowlist.

`expire_stale_jobs` runs nightly and had never expired a single job.
Live counts at the time of the fix::

    status=new       285,254
    status=expired         0

The task is correct; F99's constraint (o5j6k7l8m9n0) simply omitted
`expired`, so the UPDATE violated `ck_jobs_status_allowlist` every night,
rolled back, and re-raised into a log nobody was reading. Jobs
accumulated at `new` forever.

Impact: in a live sample of the operator's working filter, 354 of 477
jobs (74%) were past the 14-day staleness threshold, and 9 of 14
postings spot-checked against their ATS were already gone. The pool
looked large and converted badly because most of it was dead.

These tests lock the invariant that matters: **every status any code
path writes must be permitted by the constraint.** That is what broke,
and a plain "does the migration mention expired" assertion would not
have caught it.
"""
from __future__ import annotations

import ast
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
os.environ.setdefault("JWT_SECRET", "pytest-f345")

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_VERSIONS = _BACKEND / "alembic" / "versions"
_APP = _BACKEND / "app"


def _migration() -> pathlib.Path:
    matches = list(_VERSIONS.glob("*_s6t7u8v9w0x1_*.py"))
    assert len(matches) == 1, f"expected 1 F345 migration, found {matches}"
    return matches[0]


def _allowlist_from_migration() -> set[str]:
    """Read `_ALLOWED` out of the migration without importing alembic."""
    tree = ast.parse(_migration().read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_ALLOWED":
                    return {ast.literal_eval(e) for e in node.value.elts}
    raise AssertionError("_ALLOWED not found in the F345 migration")


def test_migration_chains_from_current_head():
    src = _migration().read_text()
    assert 'revision = "s6t7u8v9w0x1"' in src
    assert 'down_revision = "r5s6t7u8v9w0"' in src


def test_expired_is_now_allowed():
    assert "expired" in _allowlist_from_migration()


def test_previously_allowed_values_are_all_retained():
    """Widening must not silently drop a value — that would fail the
    ALTER on live rows and take the deploy down."""
    previous = {"new", "under_review", "accepted", "rejected", "hidden", "archived"}
    assert previous <= _allowlist_from_migration()


def _statuses_written_to_job() -> set[str]:
    """Collect every literal assigned to *Job.status* specifically.

    Deliberately narrow. A first pass scanned whole files and flagged
    `failed`/`pending`, which belong to `DiscoveryRun` -- a test that
    cries wolf on a neighbouring model gets muted, which is how the
    original bug survived in the first place.
    """
    written: set[str] = set()

    # `update(Job)` ... `.values(status="...")`. Take the slice from the
    # call up to the matching `)` of `.values(` so a nearby update() on
    # another model cannot bleed in.
    for path in _APP.rglob("*.py"):
        text = path.read_text()
        for m in re.finditer(r"update\(Job\)", text):
            tail = text[m.end():]
            vm = re.search(r"\.values\((.*?)\n\s*\)", tail, re.S)
            if vm and vm.start() < 400:
                written |= set(re.findall(r'status\s*=\s*"([a-z_]+)"', vm.group(1)))

        # Direct attribute assignment: `job.status = "..."` / `j.status = "..."`.
        written |= set(re.findall(r'\bj(?:ob)?\.status\s*=\s*"([a-z_]+)"', text))

    return written


def test_every_status_written_to_job_is_permitted():
    """The actual invariant. `expire_stale_jobs` writing a value the
    constraint rejected is precisely what broke; this catches the next
    one rather than re-catching this one.
    """
    allowed = _allowlist_from_migration()
    written = _statuses_written_to_job()

    assert "expired" in written, (
        "expected to find the expire_stale_jobs write -- the scan is "
        "probably looking in the wrong place"
    )
    unpermitted = written - allowed
    assert not unpermitted, (
        f"written to Job.status but rejected by the CHECK constraint: "
        f"{sorted(unpermitted)}"
    )


def test_the_scan_does_not_pick_up_other_models():
    """Guard the guard: DiscoveryRun's `failed`/`pending` must not leak
    in, or this test becomes noise and gets ignored."""
    written = _statuses_written_to_job()
    assert "failed" not in written
    assert "pending" not in written


def test_expire_task_still_targets_expired():
    """If someone retargets the task to 'archived' instead, this test
    should fail loudly so the constraint change gets revisited rather
    than left as dead width."""
    src = (_APP / "workers" / "tasks" / "maintenance_task.py").read_text()
    assert 'status="expired"' in src
    assert "STALE_THRESHOLD_DAYS = 14" in src


def test_filter_vocabulary_already_expected_expired():
    """Context for the fix: the read side was never the problem."""
    src = (_APP / "schemas" / "job.py").read_text()
    start = src.index("JobStatusFilter")
    assert '"expired"' in src[start:start + 400]


def test_downgrade_folds_expired_into_archived_before_narrowing():
    """Narrowing the allowlist with live `expired` rows present would
    fail the ALTER, so downgrade must migrate them first."""
    src = _migration().read_text()
    down = src[src.index("def downgrade()"):]
    assert "UPDATE jobs SET status = 'archived' WHERE status = 'expired'" in down
    fold = down.index("UPDATE jobs SET status = 'archived'")
    recreate = down.index("create_check_constraint")
    assert fold < recreate, "fold the rows before recreating the narrower constraint"
