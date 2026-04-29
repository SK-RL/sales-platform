"""Redis-backed scan concurrency locks (Finding 82).

Before this module existed, `POST /api/v1/platforms/scan/all`,
`/scan/{platform}`, `/scan/board/{board_id}`, and `/scan/discover`
all called `scan_task.delay()` with no dedup. A double-click on
"Run Full Scan" queued two tasks that each iterated 871 boards,
doubling the outbound rate to Greenhouse / Lever / Himalayas etc.
and risking HTTP 429 or an IP ban.

This module adds an atomic `SET key NX EX ttl` lock in Redis per
scan scope. The endpoint acquires before `.delay()`; if the
lock is held, it returns 409. The Celery task releases the lock
in its `finally` block, so back-to-back scans are possible once
the previous one finishes. The TTL is a safety valve: if the task
dies without releasing (process kill, unexpected exception before
`finally`, etc.), the lock auto-expires.

Two client flavors: `acquire_scan_lock` (async, for FastAPI) and
`release_scan_lock` (sync, for Celery tasks — Celery tasks run
synchronously even when the web side is async).
"""

from __future__ import annotations

import logging
from typing import Literal

import redis
from redis.asyncio import Redis as AsyncRedis

from app.config import get_settings

logger = logging.getLogger(__name__)


# TTL per scope. The TTL is a safety valve — the task's `finally`
# block releases the lock on normal completion. These ceilings are
# chosen to comfortably exceed the 95th-percentile scan duration at
# prod scale (~871 boards), so a task that genuinely hangs auto-
# expires rather than blocking the next scan forever.
_TTL_BY_SCOPE: dict[str, int] = {
    "all": 5400,           # full scan across all platforms — 90 min
    "discover": 7200,      # discovery probes unknown slugs — 2 hours
    "platform": 1800,      # single platform (subset of all) — 30 min
    "board": 300,          # single board — 5 min
    # F284 (closes F143) — admin-triggered DB backup. The backup
    # task copies a Postgres dump + alembic snapshot into
    # ``BACKUP_ROOT/<ts>/``; concurrent triggers race on the
    # timestamp directory and corrupt manifests under multi-worker
    # Celery. 10 min TTL covers the worst-case dump on the
    # documented prod scale (~50 MB pgdump + ~5 MB schema export);
    # the task's ``finally`` releases the lock on completion either
    # way.
    "backup": 600,
}


def _ttl_for(scope: str) -> int:
    """Pick a TTL based on the scope prefix. `platform:greenhouse` →
    "platform" bucket; `board:<uuid>` → "board" bucket; bare "all" /
    "discover" → their own buckets.
    """
    head = scope.split(":", 1)[0]
    return _TTL_BY_SCOPE.get(head, 1800)


def _key_for(scope: str) -> str:
    return f"scan_lock:{scope}"


async def acquire_scan_lock(scope: str) -> bool:
    """Attempt to acquire a scan lock. Returns True if acquired,
    False if another scan of the same scope is already running.

    Uses `SET key value NX EX ttl` — a single atomic command that
    sets-if-not-exists with a TTL. No race between `EXISTS` and
    `SET`. Safe to call concurrently from multiple request handlers.

    Scope naming:
      - "all": full scan of all platforms
      - "discover": platform discovery
      - "platform:<name>": per-platform scan
      - "board:<uuid>": per-board scan
    """
    settings = get_settings()
    client = AsyncRedis.from_url(settings.redis_url, decode_responses=True)
    try:
        key = _key_for(scope)
        # `nx=True` → only set if key does not exist
        # `ex=<ttl>` → auto-expire after TTL seconds
        # Return value is True on acquire, None if already held
        acquired = await client.set(key, "1", nx=True, ex=_ttl_for(scope))
        return bool(acquired)
    except Exception as e:
        # Fail-open: if Redis is unreachable, we'd rather let the scan
        # queue than block all scans indefinitely. The upstream race
        # was already the status quo before this lock existed.
        logger.warning("acquire_scan_lock(%s) failed: %s; falling open", scope, e)
        return True
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


def release_scan_lock(scope: str) -> None:
    """Release a scan lock. Safe to call from Celery tasks (sync
    context). Idempotent — a DEL on a non-existent key is a no-op.

    Called from the `finally` block of each scan task so that the
    lock is dropped on success, failure, AND retry-raise. The retry
    case means a second worker could pick up the retried task while
    the lock is free, but retries are rare and self-consistent: the
    retry target is still the same task_id, so only one instance of
    the work actually runs.
    """
    settings = get_settings()
    try:
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception as e:
        logger.warning("release_scan_lock(%s) connect failed: %s", scope, e)
        return
    try:
        client.delete(_key_for(scope))
    except Exception as e:
        logger.warning("release_scan_lock(%s) del failed: %s", scope, e)
    finally:
        try:
            client.close()
        except Exception:
            pass


ScanScope = Literal["all", "discover"]
