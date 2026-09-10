"""F343 — reopening a rejected job must not 500 on the F316 partial UNIQUE.

Live operator report, reproduced in prod: selecting 24 rejected jobs and
clicking **Reset** in the Jobs UI returned ``Request failed with status
500`` and changed NOTHING. Two of the 24 rows (Opendoor "Infrastructure
Engineer", Humana "Utilization Management Registered Nurse") each had a
second row for the same ``(company_id, lower(trim(title)))`` already
sitting in ``status='new'``.

F316's ``uq_jobs_active_company_title`` is a partial UNIQUE restricted to
the ACTIVE statuses, so flipping the rejected twin back to ``new`` raises
``IntegrityError``. Neither handler caught it, which produced two
distinct defects:

  1. **Bare 500.** The operator got no indication that the role is
     already in their queue under a different id — the one fact that
     makes the failure actionable.
  2. **All-or-nothing batch.** A single colliding row aborted the whole
     transaction, so 22 perfectly valid rows silently didn't move. This
     is the more damaging half: the UI reported failure, the user
     retried, and got the same result every time.

F343 ships ``_active_duplicates()`` — one query that maps each job about
to move onto the active row blocking it — plus:
  * ``PATCH /jobs/{id}`` → **409** naming the conflicting job id.
  * ``POST /jobs/bulk-action`` → skips only the offenders and returns
    ``{"updated": n, "skipped": [...]}`` so the UI can report the split.
  * ``IntegrityError`` still caught at commit as a TOCTOU backstop.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import uuid

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f343")

_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "api" / "v1" / "jobs.py"
).read_text()


# --- the predicate must mirror the index exactly -------------------------

def test_active_statuses_match_the_migration_predicate():
    """If these drift, the pre-check silently stops matching the DB."""
    from app.api.v1.jobs import ACTIVE_STATUSES

    migration = list(
        (pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions")
        .glob("*_m9n0o1p2q3r4_*.py")
    )[0].read_text()

    # The CREATE INDEX predicate is the source of truth.
    assert "WHERE status IN ('new', 'under_review', 'accepted')" in migration
    assert set(ACTIVE_STATUSES) == {"new", "under_review", "accepted"}


# --- _active_duplicates() ------------------------------------------------

class _FakeJob:
    def __init__(self, jid, company_id, title):
        self.id = jid
        self.company_id = company_id
        self.title = title


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Captures the single query and replays canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        return _FakeResult(self._rows)


CO_A = uuid.uuid4()
CO_B = uuid.uuid4()


def _run(jobs, target, rows):
    from app.api.v1.jobs import _active_duplicates

    db = _FakeDB(rows)
    out = asyncio.run(_active_duplicates(jobs, target, db))
    return out, db


def test_case_and_whitespace_insensitive_match():
    """The index keys on lower(trim(title)); so must the pre-check.

    This is the exact shape that made F316 necessary in the first
    place — an equality check here would miss the collision and we'd
    be back to a 500.
    """
    holder = uuid.uuid4()
    moving = _FakeJob(uuid.uuid4(), CO_A, "  Infrastructure ENGINEER ")
    blocked, _ = _run([moving], "new", [(holder, CO_A, "Infrastructure Engineer")])
    assert blocked == {moving.id: holder}


def test_row_does_not_block_itself():
    """A job already in an active status must not block its own rewrite.

    Without the `jid not in moving` guard, re-applying `new` to a row
    that is already `new` would report a phantom self-collision and the
    UI would skip a no-op the user explicitly asked for.
    """
    jid = uuid.uuid4()
    moving = _FakeJob(jid, CO_A, "SRE II")
    blocked, _ = _run([moving], "new", [(jid, CO_A, "SRE II")])
    assert blocked == {}


def test_same_title_at_a_different_company_is_not_a_collision():
    moving = _FakeJob(uuid.uuid4(), CO_A, "DevOps Engineer")
    blocked, _ = _run([moving], "new", [(uuid.uuid4(), CO_B, "DevOps Engineer")])
    assert blocked == {}


def test_non_active_target_short_circuits_without_querying():
    """Rejecting/archiving isn't gated by the partial index — pay nothing."""
    moving = _FakeJob(uuid.uuid4(), CO_A, "SRE")
    for target in ("rejected", "archived", "hidden"):
        blocked, db = _run([moving], target, [(uuid.uuid4(), CO_A, "SRE")])
        assert blocked == {}, target
        assert db.calls == 0, f"{target} should not hit the DB"


def test_partial_batch_isolates_only_the_offender():
    """The regression that mattered: 1 bad row must not sink the other 2."""
    holder = uuid.uuid4()
    ok1 = _FakeJob(uuid.uuid4(), CO_A, "Platform Engineer")
    bad = _FakeJob(uuid.uuid4(), CO_A, "Infrastructure Engineer")
    ok2 = _FakeJob(uuid.uuid4(), CO_B, "Security Engineer")

    blocked, db = _run(
        [ok1, bad, ok2], "new", [(holder, CO_A, "infrastructure engineer")]
    )
    assert blocked == {bad.id: holder}
    assert ok1.id not in blocked and ok2.id not in blocked
    assert db.calls == 1, "must stay one query regardless of batch size"


def test_null_company_id_is_skipped_not_crashed():
    moving = _FakeJob(uuid.uuid4(), None, "Orphaned Role")
    blocked, db = _run([moving], "new", [])
    assert blocked == {}
    assert db.calls == 0


# --- handler wiring ------------------------------------------------------

def test_patch_handler_raises_409_naming_the_conflict():
    assert "_active_duplicates([job], body.status, db)" in _SRC
    assert "status_code=409" in _SRC
    assert "already active in the queue under job" in _SRC


def test_bulk_handler_skips_instead_of_aborting():
    assert "applied = [j for j in jobs if j.id not in blocked]" in _SRC
    assert '"reason": "duplicate_active_listing"' in _SRC
    assert 'return {"updated": len(applied), "skipped": skipped}' in _SRC


def test_integrity_error_is_caught_in_both_handlers():
    """TOCTOU backstop — a concurrent scan can insert the twin between
    the pre-check and the commit."""
    assert "from sqlalchemy.exc import IntegrityError" in _SRC
    assert _SRC.count("except IntegrityError:") == 2
    assert _SRC.count("await db.rollback()") >= 2
