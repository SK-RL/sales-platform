"""Structural tests for the Claude Routine Apply router.

No live DB. We import the router module and inspect its FastAPI
metadata to lock down invariants that determine whether the feature
is safe to ship:

  1. Every route requires authentication. A routine endpoint without
     ``get_current_user`` in its dependency chain would let an
     unauthenticated caller trigger a run on behalf of someone else.

  2. The prefix is ``/routine``. The v6 docs + the frontend api.ts
     both bake this path in; renaming it silently is a breaking
     change we want to notice in CI.

  3. Tunables match the v6 spec. DAILY_CAP, COMPANY_COOLDOWN_DAYS,
     EXCLUDED_PLATFORMS — regression protection for anyone who
     "temporarily bumps" a constant without updating the spec.

  4. The routine accepts every geography bucket the answer_book_seed
     covers with a salary row. If the two drift (e.g. we add a new
     bucket but forget the corresponding salary seed), the routine
     can't fill salary for jobs in the new bucket and the operator
     silently gets a broken run.

End-to-end HTTP + DB coverage is covered by the live integration
harness (tests/test_api.py script-mode), not here.
"""
from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-routine")


def test_router_prefix_is_routine():
    """The frontend api.ts hardcodes ``/routine/...`` paths; the v6
    docs do too. Prefix renames must be caught at CI time."""
    from app.api.v1.routine import router

    assert router.prefix == "/routine"


def test_every_route_requires_authentication():
    """Walk every route on the router and assert its dependency
    chain includes ``get_current_user``. Operator-scoped endpoints
    MUST NOT be callable unauthenticated.

    FastAPI stores dependencies on ``route.dependant``; we recurse
    through sub-dependants to catch the common "declared via Depends
    on a parameter" case.
    """
    from app.api.deps import get_current_user
    from app.api.v1.routine import router

    def _collect_call_sites(dependant) -> set[object]:
        """Flatten every ``dependant.call`` down the tree."""
        out: set[object] = set()
        if dependant.call is not None:
            out.add(dependant.call)
        for sub in dependant.dependencies:
            out |= _collect_call_sites(sub)
        return out

    for route in router.routes:
        call_sites = _collect_call_sites(route.dependant)
        assert get_current_user in call_sites, (
            f"{route.path} [{','.join(route.methods or [])}] has no "
            f"get_current_user dependency — routine endpoints must be "
            f"authenticated"
        )


def test_tunables_match_v6_spec():
    """Constants the v6 spec + tests + docs all reference. If these
    move, the move should be deliberate — this test makes the edit
    visible in the diff."""
    from app.api.v1.routine import (
        COMPANY_COOLDOWN_DAYS,
        DAILY_CAP,
        EXCLUDED_PLATFORMS,
        ROUTINE_GEOGRAPHY_BUCKETS,
    )

    assert DAILY_CAP == 10
    assert COMPANY_COOLDOWN_DAYS == 30
    assert "linkedin" in EXCLUDED_PLATFORMS
    # Buckets must be a tuple (ordered, hashable). If someone converts
    # to a list the .index() / SQL IN semantics stay fine, but a set
    # would drop the determinism the spec asks for.
    assert isinstance(ROUTINE_GEOGRAPHY_BUCKETS, tuple)
    assert set(ROUTINE_GEOGRAPHY_BUCKETS) == {
        "global_remote", "usa_only", "uae_only"
    }


def test_routine_geography_buckets_have_salary_seed_coverage():
    """If we add a new geography bucket to the routine without seeding
    a corresponding salary-minimum answer-book entry, the routine has
    no salary answer to give for jobs in that bucket — and required-
    coverage will silently pass because the missing row isn't in the
    seed list either.

    This test pins the one-to-one relationship between the buckets
    the routine filters on and the ``expected_min_salary_*`` seed
    rows. Adding a bucket without the seed row (or vice versa) fails
    CI.
    """
    from app.api.v1.routine import ROUTINE_GEOGRAPHY_BUCKETS
    from app.services.answer_book_seed import REQUIRED_ENTRIES

    # "remote" in the seed == "global_remote" in the bucket list; the
    # routing layer normalises. "global" is a catch-all for unclassified
    # regions. Keep the explicit mapping here.
    BUCKET_TO_SEED_KEY = {
        "global_remote": "expected_min_salary_remote",
        "usa_only": "expected_min_salary_usa",
        "uae_only": "expected_min_salary_uae",
    }
    seed_keys = {qkey for (_cat, qkey, _q) in REQUIRED_ENTRIES}

    for bucket in ROUTINE_GEOGRAPHY_BUCKETS:
        expected_seed = BUCKET_TO_SEED_KEY.get(bucket)
        assert expected_seed is not None, (
            f"ROUTINE_GEOGRAPHY_BUCKETS contains {bucket!r} but this "
            f"test has no mapping for it — update BUCKET_TO_SEED_KEY"
        )
        assert expected_seed in seed_keys, (
            f"routine bucket {bucket!r} needs seed key {expected_seed!r} "
            f"but answer_book_seed.REQUIRED_ENTRIES doesn't contain it"
        )


def test_create_run_request_accepts_valid_idempotency_key():
    """Phase-2: POST /routine/runs accepts an idempotency_key for
    replay protection. The key is optional and bounded to [8, 64]
    characters so a stray empty string or a pasted-in JWT doesn't
    silently pass validation."""
    from app.schemas.routine import CreateRoutineRunRequest

    # Nominal UUID4 hex = 32 chars, well inside bounds.
    req = CreateRoutineRunRequest(
        mode="live",
        idempotency_key="a" * 32,
    )
    assert req.idempotency_key == "a" * 32

    # Omitted key is fine — legacy clients keep working.
    req_no_key = CreateRoutineRunRequest(mode="live")
    assert req_no_key.idempotency_key is None


def test_create_run_request_rejects_bad_idempotency_keys():
    """Key bounds are enforced at the Pydantic boundary so bad input
    422s before it reaches the DB. Too-short or too-long keys would
    defeat the protection (a two-char key has huge collision risk;
    a 10kB key would blow past the column width)."""
    import pytest
    from pydantic import ValidationError
    from app.schemas.routine import CreateRoutineRunRequest

    # Under minimum — 7 chars.
    with pytest.raises(ValidationError):
        CreateRoutineRunRequest(mode="live", idempotency_key="short")

    # Over maximum — 65 chars.
    with pytest.raises(ValidationError):
        CreateRoutineRunRequest(mode="live", idempotency_key="x" * 65)


def test_create_run_response_carries_replayed_flag():
    """The client needs to tell "my retry succeeded" from "a new run
    was created" — otherwise it can't decide whether to re-emit
    target_job_ids or just rejoin the existing run's stream."""
    from app.schemas.routine import CreateRoutineRunResponse
    from uuid import uuid4

    # Fresh creation defaults to replayed=False.
    fresh = CreateRoutineRunResponse(run_id=uuid4())
    assert fresh.replayed is False

    # Replay path explicitly sets it.
    replayed = CreateRoutineRunResponse(run_id=uuid4(), replayed=True)
    assert replayed.replayed is True


def test_required_coverage_response_carries_entries_and_missing():
    """Phase-2: the response schema gained an ``entries`` field with
    ALL required rows (filled + unfilled) so the UI can render + edit
    filled answers. ``missing`` is retained so a rolling-deploy
    frontend that only reads ``missing`` keeps working."""
    from app.schemas.routine import (
        RequiredCoverageEntry,
        RequiredCoverageResponse,
    )
    from uuid import uuid4

    filled = RequiredCoverageEntry(
        id=uuid4(),
        category="preferences",
        question="salary floor?",
        question_key="expected_min_salary_remote",
        answer="150000",
        filled=True,
    )
    unfilled = RequiredCoverageEntry(
        id=uuid4(),
        category="work_auth",
        question="visa?",
        question_key="visa_status",
        answer="",
        filled=False,
    )
    resp = RequiredCoverageResponse(
        complete=False,
        total_required=16,
        total_filled=1,
        missing=[unfilled],
        entries=[filled, unfilled],
    )
    assert [e.filled for e in resp.entries] == [True, False]
    assert [e.filled for e in resp.missing] == [False]


def test_update_run_emits_audit_actions():
    """B4: source-inspection guard that ``update_run`` references the
    two audit actions we rely on for "why did the run stop" forensics.
    This is structural (reads the source file) because exercising the
    real handler needs a live DB; the string-presence check catches
    accidental deletion of the audit calls during a refactor.
    """
    import inspect
    from app.api.v1 import routine as routine_mod

    src = inspect.getsource(routine_mod.update_run)
    # Mid-run kill (operator disabled the routine while it was running).
    assert "routine.run_killed_mid_flight" in src, (
        "update_run should audit mid-run kill-switch trips"
    )
    # Terminal transitions — one of these is emitted when status moves
    # from "running" to "complete" or "aborted".
    assert "routine.run_completed" in src, (
        "update_run should audit completion transitions"
    )
    assert "routine.run_aborted" in src, (
        "update_run should audit abort transitions"
    )


def test_count_recent_submissions_joins_through_live_runs():
    """A4 fix guard. ``_count_recent_submissions`` must count BOTH
    committed applies (Application.status='applied') and in-flight
    live-run submissions (ApplicationSubmission joined to a live
    RoutineRun) — counting only the first was the race window that
    let the 11th submission through.

    Structural: inspect the source for both branches. A behavioral
    test needs a live DB harness which we don't have for this router.
    """
    import inspect
    from app.api.v1 import routine as routine_mod

    src = inspect.getsource(routine_mod._count_recent_submissions)
    # Committed-applies branch.
    assert "Application.status" in src
    assert "applied_at" in src
    # In-flight branch — must join ApplicationSubmission + RoutineRun
    # and scope to live mode.
    assert "ApplicationSubmission" in src, (
        "_count_recent_submissions should union in-flight submissions"
    )
    assert 'RoutineRun.mode == "live"' in src or (
        "RoutineRun.mode" in src and '"live"' in src
    ), "in-flight branch must scope to live-mode runs only"


def test_create_run_replay_precedes_preflight_gates():
    """A5 correctness guard. The idempotency replay MUST be checked
    before any gate (kill-switch, coverage, daily-cap). Otherwise a
    retry of a request that originally succeeded would spuriously
    fail with "cap hit" because the original attempt already consumed
    a slot.

    Structural: inspect line order in the source. The
    ``body.idempotency_key`` check has to appear before the first
    ``raise HTTPException`` for any gate.
    """
    import inspect
    from app.api.v1 import routine as routine_mod

    src = inspect.getsource(routine_mod.create_run)
    replay_idx = src.find("body.idempotency_key")
    kill_gate_idx = src.find("_kill_switch_disabled(db, user.id)")
    coverage_gate_idx = src.find("_required_coverage_complete(db, user.id)")
    cap_gate_idx = src.find("_count_recent_submissions")

    assert replay_idx > -1, "replay check missing from create_run"
    assert kill_gate_idx > -1
    assert coverage_gate_idx > -1
    assert cap_gate_idx > -1
    assert replay_idx < kill_gate_idx, (
        "idempotency replay must run BEFORE the kill-switch gate"
    )
    assert replay_idx < coverage_gate_idx, (
        "idempotency replay must run BEFORE the coverage gate"
    )
    assert replay_idx < cap_gate_idx, (
        "idempotency replay must run BEFORE the daily-cap gate"
    )


def test_routine_router_registered_in_v1_router():
    """Smoke-test: the routine router must be included in the v1
    router aggregator, or none of the endpoints above are reachable
    at all."""
    from tests._routes import registered_paths

    # Mounted-app paths carry the ``/api/v1`` prefix, so routine paths
    # come out as ``/api/v1/routine/...``. We match on substring, not
    # startswith, so the test doesn't break if the v1 prefix is bumped.
    routine_paths = [p for p in registered_paths() if "/routine" in p]
    assert routine_paths, (
        "no /routine/... routes found on api_router — routine router "
        "is not registered"
    )
    # Spot-check the headline endpoints the frontend talks to. If one
    # of these is renamed, the frontend breaks silently in prod.
    joined = " ".join(routine_paths)
    for path_fragment in [
        "/routine/top-to-apply",
        "/routine/kill-switch",
        "/routine/runs",
        "/routine/humanize",
    ]:
        assert path_fragment in joined, (
            f"expected {path_fragment} on api_router — got {routine_paths}"
        )
