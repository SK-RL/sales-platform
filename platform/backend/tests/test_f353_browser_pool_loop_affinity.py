"""F353 — the browser pool is loop-bound; ``asyncio.run`` per task breaks it.

Found by running two submissions back to back inside the worker
container, which is exactly what a Celery worker does. The first
succeeded. The second **hung indefinitely** — no exception, no timeout,
just a stalled task holding a worker slot.

Mechanism:

* ``playwright_browser`` pools one Chromium per *process*.
* Both pooled handles are loop-bound — an ``asyncio.Lock`` binds to the
  loop on first await, and the browser's subprocess transport belongs to
  the loop that launched it.
* ``apply_task`` calls ``asyncio.run(...)`` once per invocation, which
  creates and then **closes** a loop each time.

So task 2 finds ``_pool.browser.is_connected() == True`` (the OS process
really is alive) and awaits a transport attached to a dead loop. That
awaits forever. The teardown symptom was visible earlier as
``RuntimeError: Event loop is closed`` from a GC'd subprocess transport,
which is what pointed here.

Two-sided fix, both pinned below:
  1. the pool discards handles owned by a different loop (defensive), and
  2. ``apply_task`` shuts the pool down *inside* its own loop, so the
     Chromium process is actually closed rather than orphaned.

(2) is what prevents a leak: (1) alone would strand a Chromium process
per application, because a dead loop can't await ``browser.close()``.
"""

import asyncio
import inspect

import app.services.playwright_browser as pb
from app.workers.tasks.apply_task import _drive


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


def _seed_pool(loop_marker):
    pb._pool.browser = _FakeBrowser()
    pb._pool.playwright = object()
    pb._pool.launch_lock = object()
    pb._pool.loop = loop_marker
    return pb._pool.browser


class TestStalePoolDiscard:
    def teardown_method(self):
        pb.reset_pool_for_tests()

    def test_handles_from_another_loop_are_dropped(self):
        """The live hang: reusing a browser whose transport belongs to a
        closed loop. is_connected() lies, so we must check the loop."""
        async def go():
            _seed_pool(loop_marker="a-dead-loop")
            pb._discard_stale_pool()
            return pb._pool.browser, pb._pool.launch_lock

        browser, lock = asyncio.run(go())
        assert browser is None
        assert lock is None

    def test_same_loop_keeps_the_pool(self):
        """Within one loop the whole point is reuse — a ~2s Chromium
        start per page would be absurd."""
        async def go():
            current = asyncio.get_running_loop()
            seeded = _seed_pool(loop_marker=current)
            pb._discard_stale_pool()
            return seeded is pb._pool.browser

        assert asyncio.run(go()) is True

    def test_first_use_records_the_owning_loop(self):
        async def go():
            pb.reset_pool_for_tests()
            pb._discard_stale_pool()
            return pb._pool.loop is asyncio.get_running_loop()

        assert asyncio.run(go()) is True

    def test_outside_a_loop_it_is_a_no_op(self):
        """Called from sync context (imports, tests) it must not raise."""
        browser = _seed_pool(loop_marker="whatever")
        pb._discard_stale_pool()
        assert pb._pool.browser is browser

    def test_two_sequential_loops_do_not_share_handles(self):
        """Reproduces the worker's shape: asyncio.run, twice."""
        async def first():
            pb._discard_stale_pool()
            _seed_pool(loop_marker=asyncio.get_running_loop())

        async def second():
            pb._discard_stale_pool()
            return pb._pool.browser

        asyncio.run(first())
        assert asyncio.run(second()) is None


class TestShutdownClearsLoopAffinity:
    def teardown_method(self):
        pb.reset_pool_for_tests()

    def test_shutdown_clears_the_loop_marker_and_lock(self):
        """Otherwise the next loop sees a stale marker and warns (or
        worse, keeps a lock bound to the closed loop)."""
        async def go():
            _seed_pool(loop_marker=asyncio.get_running_loop())
            await pb.shutdown_pool()
            return pb._pool.loop, pb._pool.launch_lock, pb._pool.browser

        loop_marker, lock, browser = asyncio.run(go())
        assert loop_marker is None
        assert lock is None
        assert browser is None

    def test_shutdown_actually_closes_the_browser(self):
        async def go():
            b = _seed_pool(loop_marker=asyncio.get_running_loop())
            await pb.shutdown_pool()
            return b.closed

        assert asyncio.run(go()) is True

    def test_shutdown_is_safe_when_nothing_launched(self):
        asyncio.run(pb.shutdown_pool())


class TestApplyTaskTearsDownInItsOwnLoop:
    def teardown_method(self):
        pb.reset_pool_for_tests()

    def test_drive_shuts_the_pool_down(self):
        """Discarding alone would orphan a Chromium process per
        application — a dead loop cannot await browser.close()."""
        called = {"n": 0}

        async def fake_shutdown():
            called["n"] += 1

        class FakeSubmitter:
            async def submit(self, **kw):
                return "outcome"

        original = pb.shutdown_pool
        pb.shutdown_pool = fake_shutdown
        try:
            out = asyncio.run(_drive(FakeSubmitter(), "https://x", [], None, True))
        finally:
            pb.shutdown_pool = original
        assert out == "outcome"
        assert called["n"] == 1

    def test_shutdown_runs_even_when_submit_raises(self):
        called = {"n": 0}

        async def fake_shutdown():
            called["n"] += 1

        class Boom:
            async def submit(self, **kw):
                raise RuntimeError("kaboom")

        original = pb.shutdown_pool
        pb.shutdown_pool = fake_shutdown
        try:
            try:
                asyncio.run(_drive(Boom(), "https://x", [], None, True))
            except RuntimeError:
                pass
        finally:
            pb.shutdown_pool = original
        assert called["n"] == 1

    def test_shutdown_failure_never_masks_the_outcome(self):
        async def bad_shutdown():
            raise RuntimeError("close failed")

        class FakeSubmitter:
            async def submit(self, **kw):
                return "outcome"

        original = pb.shutdown_pool
        pb.shutdown_pool = bad_shutdown
        try:
            assert asyncio.run(
                _drive(FakeSubmitter(), "https://x", [], None, True)
            ) == "outcome"
        finally:
            pb.shutdown_pool = original

    def test_drive_is_used_by_the_task(self):
        import app.workers.tasks.apply_task as at

        assert "_drive(" in inspect.getsource(at.submit_application_task)
