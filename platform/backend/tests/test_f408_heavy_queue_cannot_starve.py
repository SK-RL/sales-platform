"""F408 -- one heavy task must never be able to pin the heavy queue's only slot.

Prod, 2026-10-15. No new jobs had appeared since 2026-10-09 00:16 UTC.
Beat was healthy and had sent ``scan_all_platforms`` every 8 hours without a
miss. The heavy worker (one container, ``--concurrency=1``) was alive and
"busy" the whole time -- running the SAME ``enrich_company`` task over and
over. Task id ``a5e1d717`` had "succeeded" 145 times, ~3824s apiece.

Mechanism: Redis redelivers any unacked message after ``visibility_timeout``
(default 3600s). ``task_acks_late=True`` leaves a message unacked until the
task FINISHES. A task needing 3824s is therefore handed out again at 3600s
while still running; the worker starts the redelivered copy as soon as the
first one ends, and so on forever. Every scan queued behind it.

This is the second outage of the same shape in five weeks: no scan
completed 2026-09-12 -> 2026-09-25 (13 days); a different ``enrich_company``
id, ``b504aef3``, ran 178 times in that window. 19 of ~34 days with no
scanning, and nothing raised an alarm either time.

A hardening pass (F401) had already given the *periodic* tasks
``acks_late=False`` and hard time limits. It missed 9 of the 11 tasks routed
to ``heavy`` -- including ``enrich_company``, the fan-out child that ran
away. This test locks the invariant for every heavy-routed task rather than
a hand-maintained list, so the next task added to ``heavy`` cannot miss it.
"""
from __future__ import annotations

import os
import pathlib
from unittest import mock

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder"
)
os.environ.setdefault(
    "DATABASE_URL_SYNC", "postgresql://placeholder:placeholder@localhost:5432/placeholder"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f408")

_SRC = pathlib.Path(__file__).resolve().parents[1] / "app" / "workers" / "tasks" / "enrichment_task.py"


def _app():
    from app.workers.celery_app import celery_app
    import app.workers.tasks  # noqa: F401  (registers every task)
    return celery_app


def _heavy_task_names() -> list[str]:
    routes = _app().conf.task_routes
    return sorted(n for n, r in routes.items() if r.get("queue") == "heavy")


def test_there_are_heavy_tasks_to_check():
    """Guard the guard: an empty list would make every assertion below vacuous."""
    assert len(_heavy_task_names()) >= 10


def test_every_heavy_task_is_registered():
    """A routed-but-unregistered name is skipped silently by a naive audit."""
    app = _app()
    missing = [n for n in _heavy_task_names() if n not in app.tasks]
    assert not missing, f"routed to heavy but not registered: {missing}"


def test_every_heavy_task_has_a_hard_time_limit():
    app = _app()
    unbounded = [n.split(".")[-1] for n in _heavy_task_names() if not app.tasks[n].time_limit]
    assert not unbounded, f"heavy tasks with no hard time limit can pin the slot forever: {unbounded}"


def test_every_heavy_task_acks_early():
    """acks_late=True + Redis visibility timeout = redelivery while running."""
    app = _app()
    late = [n.split(".")[-1] for n in _heavy_task_names() if app.tasks[n].acks_late is not False]
    assert not late, f"heavy tasks that can be redelivered mid-run: {late}"


def test_soft_limit_leaves_room_before_the_hard_limit():
    """The soft limit exists so the task can clean up before being SIGKILLed."""
    app = _app()
    for n in _heavy_task_names():
        t = app.tasks[n]
        if t.soft_time_limit:
            assert t.soft_time_limit < t.time_limit, n


def test_visibility_timeout_exceeds_every_task_time_limit():
    """The backstop for tasks that remain acks_late=True on the default queue.
    Redelivery must only ever mean "the worker died", never "still working"."""
    app = _app()
    vt = app.conf.broker_transport_options["visibility_timeout"]
    longest = max((t.time_limit or 0) for t in app.tasks.values())
    assert longest >= 11100, "expected scan_all_platforms (11100s) to be the longest"
    assert vt > longest, f"visibility_timeout {vt}s must exceed the longest time_limit {longest}s"


def test_queue_order_strategy_is_preserved():
    """test_f406 relies on this; the new key must not have replaced it."""
    assert _app().conf.broker_transport_options.get("queue_order_strategy") == "priority"


def test_scan_all_platforms_is_still_unbounded_by_the_enrichment_limits():
    """The scan legitimately runs ~12 min (up to ~33 min observed); it must
    keep its own, much larger ceiling rather than inherit enrichment's 20."""
    t = _app().tasks["app.workers.tasks.scan_task.scan_all_platforms"]
    assert t.time_limit == 3 * 3600 + 300


# --- behaviour: enrich_company on a soft timeout --------------------------

def test_enrich_company_soft_timeout_abandons_without_retry():
    """The generic ``except Exception`` would catch SoftTimeLimitExceeded and
    retry twice, turning a 15 minute cap into 45+ minutes of the one slot."""
    from celery.exceptions import SoftTimeLimitExceeded
    from app.workers.tasks import enrichment_task as et

    company = mock.MagicMock()
    session = mock.MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = company

    with mock.patch.object(et, "SyncSession", return_value=session), \
         mock.patch("app.services.enrichment.orchestrator.run_enrichment",
                    side_effect=SoftTimeLimitExceeded()), \
         mock.patch.object(et.enrich_company, "retry") as retry:
        result = et.enrich_company.run("00000000-0000-0000-0000-000000000001")

    assert result["status"] == "timeout"
    retry.assert_not_called()
    assert company.enrichment_status == "failed"
    assert "timed out" in company.enrichment_error
    session.commit.assert_called()
    session.close.assert_called_once()


def test_soft_limit_handler_precedes_the_generic_handler():
    """Order matters: Python takes the first matching except clause."""
    src = _SRC.read_text()
    body = src[src.index("def enrich_company("):src.index("def enrich_target_companies_batch")]
    assert body.index("except SoftTimeLimitExceeded") < body.index("except Exception")
