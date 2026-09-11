# Backups — how they work and where they go

Decided by Sarthak on 2026-09-11 after 53 pre-deploy dumps (70 GB) filled the
VM's 104 GB root disk to 99%. Read this before touching anything under
`backups/`, the deploy script, or the nightly Celery task.

## The one backup stream: nightly, on the VM, keep 3

| | |
|---|---|
| What | Full `pg_dump` of `jobplatform` (custom format `jobplatform.pgdump` + `jobplatform.sql.gz` + `manifest.json` + `checksums.sha256`) |
| Who | Celery task `app.workers.tasks.backup_task.run_backup` (beat: `nightly_backup`) |
| When | Every day 03:00 UTC |
| Where | Docker volume `sales-platform_backups`, mounted at `/app/backups` in the worker; on the host: `/var/lib/docker/volumes/sales-platform_backups/_data/<YYYYMMDD_HHMMSS>/` |
| Retention | `BACKUP_KEEP_LAST=3` (docker-compose.prod.yml + code default). `_rotate()` runs at the end of each backup and deletes older dated directories. |
| On demand | `POST /api/v1/monitoring/backup` (Monitoring page → "Backup now", label it). Lands in the same rotated set. Do this before a risky migration. |
| Size | ~1.3 GB per copy (Sept 2026); grows with `job_descriptions`. |
| Incomplete copies | A directory **without** `manifest.json` is a backup that was interrupted (usually a worker restart mid-run). Safe to delete; the laptop pull skips them. |

**Pre-deploy dumps are retired.** `platform/scripts/ci-deploy.sh` no longer
runs `pg_dump` before a deploy; it sweeps any leftover
`backups/pre-deploy-*.sql.gz` on the host (`remove_pre_deploy_dumps`). Do not
reintroduce a per-deploy dump: at 10+ deploys a day it was 15 GB/day, and
the VM's copy of the deploy script is **not shipped by deploys** (see
`docs/DEPLOY_SETUP.md`), so any retention rule added there only takes effect
after someone copies the script to `/opt/sales-platform/scripts/ci-deploy.sh`
by hand.

## Off-VM copy: weekly to Sarthak's laptop, keep 3

The VM backups live on the same disk as the database, so they are rollback
points, not disaster recovery. The off-VM copy is a launchd job on the laptop.

| | |
|---|---|
| Job | launchd `com.reventlabs.sales-platform.backup-pull` (`~/Library/LaunchAgents/com.reventlabs.sales-platform.backup-pull.plist`) — the Mac's cron; a slot missed while asleep runs on wake |
| When | Every Sunday 10:00 local |
| Script | `platform/scripts/pull-backup-to-laptop.sh`, installed as a copy at `~/.local/bin/sales-platform-pull-backup.sh` (the repo checkout is shared by several Claude sessions that switch branches, so the job must not depend on which branch is checked out) |
| Does | ssh to the VM, pick the newest backup directory that has a `manifest.json`, stream `jobplatform.pgdump` + `manifest.json` + `checksums.sha256` (skips the duplicate `.sql.gz`), verify the dump's SHA-256 against `checksums.sha256`, then keep only the 3 newest dated directories locally |
| Where | `<repo>/db-backups/<YYYYMMDD_HHMMSS>/` — gitignored. Loose files there (e.g. an older `*.dump`) are never touched by rotation |
| Log | `<repo>/db-backups/pull.log` |
| Speed | ~1 MB/s from the VM (Oracle free-tier egress) → ~20 min per copy |
| Access | `ssh -i ~/.ssh/Sarthak-Betaque -o IdentitiesOnly=yes ubuntu@161.118.207.119` (passwordless sudo). The `deploy` user's key is a forced-command key and cannot do this. |
| Reinstall / run now | `bash platform/scripts/install-laptop-backup-job.sh` from main; `bash ~/.local/bin/sales-platform-pull-backup.sh` to pull immediately |

## Restore

`platform/scripts/restore.sh <backup-dir>` — needs `jobplatform.pgdump`
(uses `pg_restore`). Run it on the VM against the postgres container, or
point it at a laptop copy after `scp`-ing that directory back up.

## Disk hygiene that goes with this

- Deploys prune container images to the 3 newest tags per repo (in `ci-deploy.sh`).
- `journalctl --vacuum-size=300M --vacuum-time=7d` was applied once on 2026-09-11; the journal had reached 3.4 GB.
- Check: `GET /api/v1/monitoring/vm` → `disk` and `backups` (count + total size on the host), and the `disk_fs` guardrail turns critical at 95%.
