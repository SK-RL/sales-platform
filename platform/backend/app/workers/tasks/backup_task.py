"""Automated database backup task.

Runs nightly via Celery beat. Executes pg_dump inside the postgres container,
writes timestamped backup + manifest + checksums to /app/backups/, rotates old
files, and records the outcome in the scan_logs table.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from celery import shared_task

from app.workers.tasks._db import SyncSession
from app.models.scan import ScanLog

logger = logging.getLogger(__name__)

# ── configuration ─────────────────────────────────────────────────────────────
BACKUP_ROOT = Path(os.getenv("BACKUP_DIR", "/app/backups"))
KEEP_LAST   = int(os.getenv("BACKUP_KEEP_LAST", "14"))
PG_HOST     = os.getenv("POSTGRES_HOST", "postgres")
PG_PORT     = os.getenv("POSTGRES_PORT", "5432")
PG_DB       = os.getenv("POSTGRES_DB",  "jobplatform")
PG_USER     = os.getenv("POSTGRES_USER", "postgres")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")


# ── helpers ───────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pg_env() -> dict:
    env = os.environ.copy()
    env["PGPASSWORD"] = PG_PASSWORD
    return env


def _row_counts(env: dict) -> list[dict]:
    """Return [{table, rows}] from pg_stat_user_tables."""
    sql = (
        "SELECT json_agg(row_to_json(t)) FROM ("
        "  SELECT relname AS table, n_live_tup::int AS rows"
        "  FROM pg_stat_user_tables ORDER BY relname"
        ") t;"
    )
    result = subprocess.run(
        ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
         "-t", "-A", "-c", sql],
        env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        raw = result.stdout.strip()
        return json.loads(raw) if raw and raw != "null" else []
    return []


def _alembic_rev(env: dict) -> str:
    result = subprocess.run(
        ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
         "-t", "-A", "-c", "SELECT version_num FROM alembic_version;"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _rotate(backup_root: Path, keep: int) -> int:
    """Rotate stale backups, keeping the most recent ``keep`` of each kind.

    Two backup shapes coexist in this directory:

    * **Nightly backups** — dated subdirs like ``20260426_030000/`` written
      by ``run_backup`` (this task). Sortable by name = sortable by time.
    * **Pre-deploy backups** — flat ``pre-deploy-<sha>.sql.gz`` files
      written by ``scripts/ci-deploy.sh:pre_deploy_backup`` before each
      rolling restart. Sortable by mtime = sortable by deploy time.

    Pre-fix this function only rotated the dated subdirs. The flat
    pre-deploy files accumulated unbounded — live monitoring on
    2026-04-26 showed **77 backups occupying 13.3 GB** with the oldest
    going back 20 days (one per deploy, multiple per busy day). At the
    free-tier 200 GB cap this would fill the disk in ~3 months.

    Now we rotate BOTH shapes with the same ``keep`` count. Order is:
      1. Subdirs (nightly + manual labels that begin with a digit)
      2. ``pre-deploy-*.sql.gz`` flat files

    Failure on either path is logged but doesn't abort — half-rotated
    is strictly better than not-rotated when disk is filling.
    """
    removed = 0

    # 1. Dated subdirs — the original behaviour.
    try:
        dirs = sorted(
            [d for d in backup_root.iterdir() if d.is_dir() and d.name[0].isdigit()],
            reverse=True,
        )
        for old in dirs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
            logger.info("Rotated old backup dir: %s", old.name)
            removed += 1
    except Exception as exc:
        logger.warning("backup-rotate: subdir sweep failed: %s", exc)

    # 2. Flat pre-deploy .sql.gz files — written by ci-deploy.sh, NOT by
    # this task. Sort by mtime so we keep the most recent ``keep``
    # regardless of the embedded short-sha (sha-tags don't sort
    # chronologically — ``sha-aa6017d`` is alphabetically before
    # ``sha-15cf095`` despite being newer).
    try:
        flats = sorted(
            backup_root.glob("pre-deploy-*.sql.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in flats[keep:]:
            try:
                old.unlink()
                logger.info("Rotated old pre-deploy backup: %s", old.name)
                removed += 1
            except OSError as exc:
                logger.warning("Could not unlink %s: %s", old.name, exc)
    except Exception as exc:
        logger.warning("backup-rotate: flat-file sweep failed: %s", exc)

    return removed


# ── main task ─────────────────────────────────────────────────────────────────

@shared_task(name="app.workers.tasks.backup_task.run_backup", bind=True, max_retries=1)
def run_backup(self, label: str = "scheduled") -> dict:
    """Full pg_dump backup with manifest, checksums, and rotation.

    F284 (closes F143) — releases the ``scan_lock:backup`` Redis
    lock in the outermost ``finally`` so back-to-back manual +
    nightly backups are possible once one finishes. The endpoint
    in monitoring.py acquires the lock before queueing; if this
    task crashes before reaching the release, the lock's TTL (10
    min) is the safety valve.
    """
    from app.utils.scan_lock import release_scan_lock

    started = datetime.now(timezone.utc)
    ts      = started.strftime("%Y%m%d_%H%M%S")
    dest    = BACKUP_ROOT / ts
    dest.mkdir(parents=True, exist_ok=True)

    env = _pg_env()
    status = "ok"
    error_msg = ""
    dump_bytes = 0

    try:
        # 1. Row counts before dump
        table_counts = _row_counts(env)
        total_rows   = sum(t["rows"] for t in table_counts)
        alembic_rev  = _alembic_rev(env)

        # 2. pg_dump custom format (compressed, fast restore)
        dump_file = dest / "jobplatform.pgdump"
        result = subprocess.run(
            [
                "pg_dump",
                "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
                "--format=custom", "--compress=9", "--no-password",
                "-f", str(dump_file),
            ],
            env=env, capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {result.stderr[:500]}")
        dump_bytes = dump_file.stat().st_size
        logger.info("pg_dump complete: %s (%.1f MB)", dump_file.name, dump_bytes / 1024 / 1024)

        # 3. Plain SQL gzip (human-readable emergency copy)
        sql_file = dest / "jobplatform.sql.gz"
        pg_proc  = subprocess.Popen(
            ["pg_dump", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
             "--format=plain", "--no-password"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with gzip.open(sql_file, "wb", compresslevel=9) as gz_out:
            for chunk in pg_proc.stdout:  # type: ignore[union-attr]
                gz_out.write(chunk)
        pg_proc.wait(timeout=600)
        if pg_proc.returncode != 0:
            raise RuntimeError(f"pg_dump (SQL) failed: {pg_proc.stderr.read().decode()[:300]}")  # type: ignore[union-attr]
        logger.info("SQL dump complete: %s (%.1f MB)", sql_file.name, sql_file.stat().st_size / 1024 / 1024)

        # 4. Checksums
        checksum_file = dest / "checksums.sha256"
        checksums = {
            "jobplatform.pgdump": _sha256(dump_file),
            "jobplatform.sql.gz": _sha256(sql_file),
        }
        with open(checksum_file, "w") as fh:
            for name, sha in checksums.items():
                fh.write(f"{sha}  {name}\n")

        # 5. Manifest
        manifest = {
            "timestamp":       ts,
            "created_at":      started.isoformat(),
            "label":           label,
            "database":        PG_DB,
            "alembic_revision": alembic_rev,
            "total_rows":      total_rows,
            "dump_size_bytes": dump_bytes,
            "sql_gz_size_bytes": sql_file.stat().st_size,
            "table_row_counts": table_counts,
            "checksums":       checksums,
        }
        with open(dest / "manifest.json", "w") as fh:
            json.dump(manifest, fh, indent=2)

        # 6. Rotation
        removed = _rotate(BACKUP_ROOT, KEEP_LAST)
        logger.info("Backup %s complete — %d rows, %d old removed", ts, total_rows, removed)

    except Exception as exc:
        status    = "error"
        error_msg = str(exc)
        logger.exception("Backup failed: %s", exc)
        # Partial backup dir cleanup
        if dest.exists() and not any(dest.iterdir()):
            shutil.rmtree(dest, ignore_errors=True)

    # 7. Record in scan_logs
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    with SyncSession() as db:
        log_entry = ScanLog(
            platform="backup",
            board_slug=ts,
            jobs_found=0,
            jobs_new=0,
            jobs_updated=0,
            status=status,
            error_message=error_msg if error_msg else None,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(log_entry)
        db.commit()

    # F284: release the lock unconditionally — success, failure,
    # AND the early-return path implicit in any future refactor.
    # ``release_scan_lock`` is a no-op on a non-existent key so a
    # double-release is safe.
    release_scan_lock("backup")

    return {
        "timestamp":   ts,
        "status":      status,
        "dump_bytes":  dump_bytes,
        "elapsed_s":   round(elapsed, 1),
        "error":       error_msg,
    }
