#!/usr/bin/env bash
# =============================================================================
# pull-backup-to-laptop.sh — copy the newest COMPLETE nightly backup from the
# VM to this laptop, verify its checksum, keep only the last N copies.
#
# Sarthak, 2026-09-11: "Create a local cron job on the laptop and keep only
# last 3 backups." Runs weekly from launchd (see install-laptop-backup-job.sh);
# safe to run by hand any time:  bash platform/scripts/pull-backup-to-laptop.sh
#
# What it copies: jobplatform.pgdump + manifest.json + checksums.sha256 from
# /var/lib/docker/volumes/sales-platform_backups/_data/<timestamp>/ on the VM
# (the worker's nightly backup, BACKUP_KEEP_LAST=3 there). The duplicate
# jobplatform.sql.gz is skipped — restore.sh only needs the .pgdump.
# A directory without manifest.json is a backup that was interrupted; skipped.
# =============================================================================
set -euo pipefail

VM_HOST="${VM_HOST:-ubuntu@161.118.207.119}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/Sarthak-Betaque}"
# Where copies land. launchd passes DEST (the repo's gitignored db-backups/);
# run by hand from the repo it defaults to the same place.
DEST="${DEST:-$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)/db-backups}"
KEEP="${KEEP:-3}"
REMOTE_ROOT="/var/lib/docker/volumes/sales-platform_backups/_data"
SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20 "$VM_HOST")

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

mkdir -p "$DEST"
log "Looking for the newest complete backup on $VM_HOST"
latest="$("${SSH[@]}" "for d in \$(sudo ls -1 $REMOTE_ROOT | grep -E '^[0-9]{8}_[0-9]{6}\$' | sort -r); do sudo test -f $REMOTE_ROOT/\$d/manifest.json && { echo \$d; break; }; done")"
[[ -n "$latest" ]] || { log "No complete backup found on the VM"; exit 1; }

if [[ -f "$DEST/$latest/manifest.json" && -f "$DEST/$latest/jobplatform.pgdump" ]]; then
  log "Already have $latest — nothing to copy"
else
  tmp="$DEST/.incoming-$latest"
  rm -rf "$tmp"; mkdir -p "$tmp"
  log "Copying $latest (pgdump + manifest + checksums)…"
  # Plain tar: the .pgdump is already compressed, gzip on the 2-core VM only slows the stream down.
  "${SSH[@]}" "sudo tar cf - -C $REMOTE_ROOT/$latest jobplatform.pgdump manifest.json checksums.sha256" | tar xf - -C "$tmp"
  # Verify the dump against the VM's own checksum list.
  expected="$(grep 'jobplatform.pgdump' "$tmp/checksums.sha256" | awk '{print $1}')"
  actual="$(shasum -a 256 "$tmp/jobplatform.pgdump" | awk '{print $1}')"
  if [[ -z "$expected" || "$expected" != "$actual" ]]; then
    rm -rf "$tmp"
    log "CHECKSUM MISMATCH for $latest (expected $expected, got $actual) — copy discarded"
    exit 2
  fi
  mv "$tmp" "$DEST/$latest"
  log "Copied and verified $latest ($(du -sh "$DEST/$latest" | cut -f1))"
fi

# Rotation: keep the newest $KEEP dated backup directories; loose files are left alone.
removed=0
for d in $(ls -1 "$DEST" | grep -E '^[0-9]{8}_[0-9]{6}$' | sort -r | tail -n +"$((KEEP + 1))"); do
  rm -rf "$DEST/$d" && removed=$((removed + 1))
done
log "Local copies: $(ls -1 "$DEST" | grep -cE '^[0-9]{8}_[0-9]{6}$') (kept $KEEP, removed $removed) in $DEST"
