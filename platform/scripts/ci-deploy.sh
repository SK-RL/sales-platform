#!/usr/bin/env bash
# =============================================================================
# ci-deploy.sh — VM-side deploy orchestrator, invoked over SSH by CI
#
# Installed at: /opt/sales-platform/scripts/ci-deploy.sh (mode 0755, owner deploy)
# Called via forced-command SSH (authorized_keys command="...") — so any
# command string the client sends arrives in $SSH_ORIGINAL_COMMAND. We parse
# that ourselves and only allow a strict subset of actions.
#
# Supported actions:
#   deploy <TAG> [GHCR_USER]   -- pull new GHCR images, run migrations, rolling
#                                 restart, health-check, auto-rollback on fail.
#                                 If GHCR_USER is passed, the script reads one
#                                 line from stdin as a GHCR pull token and
#                                 `docker login`s before pulling (so private
#                                 packages work with no long-lived creds).
#   rollback <TAG> [GHCR_USER] -- swap RELEASE_TAG to a prior tag, restart.
#                                 Same optional GHCR_USER + stdin-token.
#   status                     -- print compose ps + last-deploy.json
#   stage-infra                -- read a tarball from stdin (gzipped, ≤10 MiB)
#                                 and stage it at /tmp/sales-platform-infra.tgz.
#                                 The next `deploy` run consumes + extracts it
#                                 via sync_infra_tarball before pulling images.
#                                 Lets CI ship docker-compose.yml / scripts /
#                                 nginx config changes alongside the image
#                                 update, so infra-only edits actually take
#                                 effect (pre-2026-04-17 they didn't).
#
# Design rules:
#   - Tag must match ^[a-zA-Z0-9_.-]+$ (defeats shell injection)
#   - All state written to /opt/sales-platform: .env (RELEASE_TAG=...),
#     .last-deploy.json, .prev-release
#   - Logs to /opt/sales-platform/logs/ci-deploy.log
# =============================================================================
set -euo pipefail

APP_DIR="/opt/sales-platform"
COMPOSE="docker compose -f docker-compose.prod.yml"
LOG_DIR="$APP_DIR/logs"
LOG_FILE="$LOG_DIR/ci-deploy.log"
# GHCR image pull paths. Default owner tracks the GitHub org that owns
# the packages — changed from ``betaque-team`` to ``sk-rl`` after the
# 2026-07-31 org rename (GHCR does not redirect package paths on rename,
# so the old path 404s and every deploy failed at the image pull).
# Overridable via env for staging / future renames. NOTE: this script
# is installed at /opt/sales-platform/scripts/ci-deploy.sh and is NOT
# shipped by the stage-infra tarball — so a repo edit here only takes
# effect after the VM copy is refreshed (see DEPLOY.md).
GHCR_IMAGE_BACKEND="${GHCR_IMAGE_BACKEND:-ghcr.io/sk-rl/sales-platform/backend}"
GHCR_IMAGE_FRONTEND="${GHCR_IMAGE_FRONTEND:-ghcr.io/sk-rl/sales-platform/frontend}"

mkdir -p "$LOG_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }
die() { log "ERROR: $*"; exit 1; }

validate_tag() {
  [[ "${1:-}" =~ ^[a-zA-Z0-9_.-]+$ ]] || die "Invalid tag format: '${1:-}' (allowed: alphanumeric, dot, dash, underscore)"
  [[ "${#1}" -le 64 ]] || die "Tag too long (>64 chars)"
}

validate_ghcr_user() {
  # GitHub usernames: alphanumeric + hyphen, 1-39 chars
  [[ "${1:-}" =~ ^[a-zA-Z0-9-]{1,39}$ ]] || die "Invalid GHCR user: '${1:-}'"
}

# -----------------------------------------------------------------------------
# If a GHCR user is supplied, read a single-line token from stdin and log in
# to ghcr.io. The token lives only in the docker config (600, deploy-owned)
# for the duration of the deploy, then we `docker logout` at the end.
# -----------------------------------------------------------------------------
ghcr_login_from_stdin() {
  local user="$1"
  validate_ghcr_user "$user"
  local token
  # Read exactly one line; fail fast if nothing arrives within 10 s
  if ! IFS= read -rs -t 10 token; then
    die "Expected GHCR token on stdin (within 10s) but got none"
  fi
  [[ -n "$token" ]] || die "Empty GHCR token on stdin"
  # Feed the token via stdin to docker login (no process-listing exposure)
  if echo "$token" | docker login ghcr.io -u "$user" --password-stdin >/dev/null 2>&1; then
    log "ghcr login OK (user=$user)"
  else
    die "ghcr login failed"
  fi
  unset token
}

ghcr_logout() {
  docker logout ghcr.io >/dev/null 2>&1 || true
}

# -----------------------------------------------------------------------------
# Regression finding 234 (critical, F234): the prior ci-deploy.sh only read
# line 1 of stdin (the GHCR token via `ghcr_login_from_stdin`). deploy.yml
# (commit 0261ac1) writes ANTHROPIC_API_KEY on line 2 expecting the VM-side
# script to consume it — but those bytes fell into the void on connection
# close. Result: every deploy since 0261ac1 reported success with all CI
# logs green, but the key never reached /opt/sales-platform/platform/.env,
# so all three AI features (resume customize, cover-letter, interview-prep)
# returned "Contact an administrator" in prod with no signal that the
# Secret-was-set / VM-state-empty mismatch existed.
#
# This helper closes the gap. Called RIGHT AFTER `ghcr_login_from_stdin` in
# `action_deploy` and `action_rollback`. Reads one line (with a short
# timeout — deploy.yml always sends both lines back-to-back, so a slow read
# means the key wasn't piped). Empty value = "leave .env unchanged" per the
# documented contract (deploy.yml comment lines 274-279). A non-empty value
# replaces any existing `ANTHROPIC_API_KEY=` line in .env, atomically via a
# tmp file + `mv -f` so a partial write can't corrupt the file mid-deploy.
# Mode 600 on the tmp file before the move so the key never has a window
# of world-readability.
#
# The .env file is mounted into the backend container by docker-compose,
# so the next `$COMPOSE up -d --no-deps backend` (already in `action_deploy`)
# picks up the new value on container restart.
persist_anthropic_key_from_stdin() {
  # F234 (Round 65 hotfix): the live VM has `.env` at $APP_DIR/.env
  # (verified: only `/opt/sales-platform/.env` exists; no
  # `platform/.env`). The earlier Round 63 helper wrote the wrong path
  # and silently no-op'd on every deploy with the WARN message
  # `.env not found at /opt/sales-platform/platform/.env`. Fix: use
  # the same path that `set_release_tag` and the rest of this script
  # already use (cd "$APP_DIR" && touch .env). Conflict resolved by
  # taking HEAD — feat branch had the wrong pre-hotfix path.
  local env_file="$APP_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    log "WARN: .env not found at $env_file — refusing to persist ANTHROPIC_API_KEY"
    # Drain the line so it doesn't sit on stdin and break later reads.
    local _drain
    IFS= read -rs -t 2 _drain 2>/dev/null || true
    return 0
  fi
  local anthropic_key=""
  # 5 s timeout: deploy.yml always emits the key right after the GHCR
  # token, so a real send arrives in <100 ms. A timeout here means the
  # key was either omitted (e.g. running this script manually) or the
  # caller hung up early — both safe to treat as "no change".
  if IFS= read -rs -t 5 anthropic_key 2>/dev/null && [[ -n "$anthropic_key" ]]; then
    local tmp
    tmp="$(mktemp)"
    chmod 600 "$tmp"
    # Strip any existing ANTHROPIC_API_KEY line (idempotent — multi-deploy
    # re-runs don't accrete duplicates), then append the new value.
    grep -v '^ANTHROPIC_API_KEY=' "$env_file" > "$tmp" || true
    printf 'ANTHROPIC_API_KEY=%s\n' "$anthropic_key" >> "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$env_file"
    log "ANTHROPIC_API_KEY persisted to .env (length=${#anthropic_key})"
    unset anthropic_key
  else
    log "ANTHROPIC_API_KEY: empty or unset on stdin — leaving .env unchanged"
  fi
}

# Generic sibling of persist_anthropic_key_from_stdin for secrets piped
# AFTER ANTHROPIC_API_KEY. deploy.yml's stdin contract for the `deploy`
# verb is, in order:
#   line 1 GHCR token · line 2 ANTHROPIC_API_KEY ·
#   line 3 ADZUNA_APP_ID · line 4 ADZUNA_APP_KEY
# Reads ONE line and upserts ``VAR=<value>`` into .env atomically (0600).
# Empty value / 5 s timeout = "leave .env unchanged" (same contract as
# the ANTHROPIC helper) so a manual run — or an older deploy.yml that
# sends fewer lines — is safe and never wipes a provisioned value.
persist_env_var_from_stdin() {
  local var_name="$1"
  local env_file="$APP_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    log "WARN: .env not found at $env_file — refusing to persist $var_name"
    local _drain
    IFS= read -rs -t 2 _drain 2>/dev/null || true
    return 0
  fi
  local val=""
  if IFS= read -rs -t 5 val 2>/dev/null && [[ -n "$val" ]]; then
    local tmp
    tmp="$(mktemp)"
    chmod 600 "$tmp"
    grep -v "^${var_name}=" "$env_file" > "$tmp" || true
    printf '%s=%s\n' "$var_name" "$val" >> "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$env_file"
    log "$var_name persisted to .env (length=${#val})"
    unset val
  else
    log "$var_name: empty or unset on stdin — leaving .env unchanged"
  fi
}

# -----------------------------------------------------------------------------
# Set RELEASE_TAG in .env (persisted for future compose commands) and export
# -----------------------------------------------------------------------------
set_release_tag() {
  local tag="$1"
  cd "$APP_DIR"
  if grep -q "^RELEASE_TAG=" .env 2>/dev/null; then
    sed -i.bak "s|^RELEASE_TAG=.*|RELEASE_TAG=${tag}|" .env && rm -f .env.bak
  else
    echo "RELEASE_TAG=${tag}" >> .env
  fi
  export RELEASE_TAG="$tag"
}

# -----------------------------------------------------------------------------
# Read currently-deployed tag from .env (falls back to "latest")
# -----------------------------------------------------------------------------
current_tag() {
  grep -E "^RELEASE_TAG=" "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | head -1 || echo "latest"
}

# -----------------------------------------------------------------------------
# Backup DB + app state before a deploy. Non-fatal if backup script missing.
# -----------------------------------------------------------------------------
# Retention for pre-deploy dumps. Each dump is a full gzip of the database
# (~1.2GB and growing), and pre-fix NOTHING ever removed them: 144 dumps
# dating back to April had accumulated to 52GB — over half the 97GB root
# disk and the single largest consumer on the VM, dwarfing the 4.2GB
# database they back up. At ~2-4 deploys/week the disk would have hit 100%
# within a month, which takes Postgres (and every other service) down with
# it.
#
# Retention is age-based (keep anything newer than N days) with a hard
# floor on COUNT. The floor matters: age alone would wipe out every
# restore point during any quiet stretch longer than the window — a week
# without deploys and the next deploy would delete all history including
# the dump it just took. Whichever rule keeps more files wins.
#
# NOTE: these dumps live on the SAME disk as the database, so they are
# rollback points, not disaster recovery — losing the volume loses both.
# An offsite copy is still needed; retention here only stops the bleed.
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
BACKUP_RETENTION_MIN="${BACKUP_RETENTION_MIN:-3}"

prune_old_backups() {
  local dir="$APP_DIR/backups"
  [ -d "$dir" ] || return 0
  local keep old removed=0

  # Newest-first list of the N we always keep regardless of age (the
  # floor). Glob matches only `pre-deploy-*.sql.gz`, so hand-made
  # full-dump directories under backups/ are never touched. `set -o
  # pipefail` makes an empty pipe fatal, so absorb with `|| true`.
  # Filenames are sha-derived — no spaces to word-split on.
  keep="$(ls -1t "$dir"/pre-deploy-*.sql.gz 2>/dev/null | head -n "$BACKUP_RETENTION_MIN" || true)"

  # Candidates: older than the age window. Anything in `keep` is then
  # excluded, so the floor always wins over the age rule.
  old="$(find "$dir" -maxdepth 1 -name 'pre-deploy-*.sql.gz' -mtime "+$BACKUP_RETENTION_DAYS" 2>/dev/null || true)"
  [ -n "$old" ] || { log "No pre-deploy backups older than ${BACKUP_RETENTION_DAYS}d to prune"; return 0; }

  while IFS= read -r f; do
    [ -n "$f" ] || continue
    # Skip if this file is one of the floor-protected newest dumps.
    if printf '%s\n' "$keep" | grep -Fxq "$f"; then continue; fi
    rm -f "$f" && removed=$((removed + 1)) || true
  done <<< "$old"

  log "Pruned $removed pre-deploy backup(s) older than ${BACKUP_RETENTION_DAYS}d (floor: newest $BACKUP_RETENTION_MIN always kept)"
}

pre_deploy_backup() {
  local label="$1"
  cd "$APP_DIR"
  local out="$APP_DIR/backups/pre-deploy-${label}.sql.gz"
  mkdir -p "$APP_DIR/backups"
  log "Taking pre-deploy DB backup -> $out"
  if $COMPOSE exec -T postgres pg_dump -U postgres jobplatform 2>/dev/null | gzip > "$out"; then
    log "Backup OK ($(du -h "$out" | cut -f1))"
    # Prune only AFTER a confirmed-good new dump, never before — a failed
    # pg_dump must not leave us with fewer restore points than we started.
    prune_old_backups
  else
    log "WARN: pg_dump failed; continuing anyway"
    rm -f "$out"
  fi
}

# -----------------------------------------------------------------------------
# Wait up to N seconds for backend to report healthy
# -----------------------------------------------------------------------------
wait_for_healthy() {
  local service="$1" timeout="${2:-60}"
  local waited=0
  cd "$APP_DIR"
  while (( waited < timeout )); do
    local status
    status="$($COMPOSE ps "$service" --format '{{.Status}}' 2>/dev/null || true)"
    if echo "$status" | grep -q "healthy"; then
      log "$service is healthy after ${waited}s"
      return 0
    fi
    # Accept "Up" for services without healthcheck
    if [[ "$service" != "backend" ]] && echo "$status" | grep -q "^Up"; then
      log "$service is up after ${waited}s"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

# -----------------------------------------------------------------------------
# Verify backend responds on its internal port (401 on monitoring is OK — means
# auth is enforced and the app is serving)
# -----------------------------------------------------------------------------
backend_http_check() {
  local code
  code="$(curl -o /dev/null -s -w "%{http_code}" --max-time 10 http://localhost:8000/api/v1/monitoring || echo "000")"
  [[ "$code" == "200" || "$code" == "401" ]]
}

# -----------------------------------------------------------------------------
# Sync infra files (compose YAML, helper scripts, nginx config) from the
# tarball CI just SCP'd to /tmp/sales-platform-infra.tgz.
#
# Why this exists
# ---------------
# Until 2026-04-17 the deploy pipeline only updated container *images*. Any
# change to docker-compose.prod.yml, infra/scripts/*.sh, or infra/nginx/**
# landed in git, shipped nowhere, and silently failed on the VM until
# someone noticed (e.g. the host-metrics bind-mount was added to the
# git compose file on Apr 15 but never reached the deployed file — the
# VM Health panel had been showing "unavailable" for two days).
#
# The tarball is built by deploy.yml just before the SSH deploy step:
#   tar czf - -C platform docker-compose.prod.yml docker-compose.tunnel.yml \
#       infra/scripts infra/nginx | ssh ... "cat > /tmp/sales-platform-infra.tgz"
#
# Safety:
# - Tarball produced from a known repo path (no user-controlled content
#   inside the archive). We still validate every entry against an
#   allowlist of relative paths to stay safe against a compromised CI.
# - tar `--no-same-owner --no-overwrite-dir` so the existing dir perms
#   don't get clobbered.
# - `cmp -s` before reinstalling host scripts to /usr/local/bin so we
#   don't bump mtime + restart cron's mental model unnecessarily.
# - Missing tarball is a no-op (manual `status` calls or back-compat
#   with old CI runs that pre-date this code path).
# -----------------------------------------------------------------------------
sync_infra_tarball() {
  local tar_path="/tmp/sales-platform-infra.tgz"
  if [[ ! -f "$tar_path" ]]; then
    log "sync_infra: $tar_path not present, skipping (legacy deploy or manual run)"
    return 0
  fi

  # Validate that every file in the tarball is on the allow-list. Reject
  # absolute paths, `..` traversal, and anything outside the four expected
  # subtrees. Bails the deploy entirely if validation fails — better a
  # blocked deploy than a malicious file landing in /opt/sales-platform.
  local listing
  listing="$(tar tzf "$tar_path" 2>/dev/null)" || die "sync_infra: tarball is corrupt or unreadable"
  local entry
  while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    if [[ "$entry" =~ ^/ ]] || [[ "$entry" == *..* ]]; then
      die "sync_infra: rejecting unsafe path in tarball: $entry"
    fi
    case "$entry" in
      docker-compose.prod.yml|docker-compose.tunnel.yml) ;;
      infra/|infra/scripts/|infra/scripts/*|infra/nginx/|infra/nginx/*) ;;
      *)
        die "sync_infra: rejecting out-of-allowlist path: $entry"
        ;;
    esac
  done <<< "$listing"

  log "sync_infra: extracting $(echo "$listing" | wc -l | tr -d ' ') entries to $APP_DIR"
  tar xzf "$tar_path" -C "$APP_DIR" --no-same-owner --no-overwrite-dir \
    || die "sync_infra: tar extraction failed"

  # Make sure scripts are executable — tar preserves mode but a CI
  # archive built without `--mode` could ship them un-x. Cheap chmod is
  # idempotent and recovers from that failure mode.
  chmod +x "$APP_DIR"/infra/scripts/*.sh 2>/dev/null || true

  # Cron MUST point at the in-tree path so future deploys auto-update
  # the script content (no /usr/local/bin/* copy step needed — that
  # would require passwordless sudo we don't grant the deploy user).
  # See docs/VM_MONITORING.md for the one-time crontab fixup; this
  # function logs (but does not fix) cron entries that still target
  # /usr/local/bin/, since editing root's crontab also needs sudo.
  if sudo -n true 2>/dev/null; then
    # Best-effort: if passwordless sudo IS available (some VMs), drop
    # in symlinks so the legacy crontab keeps working transparently.
    for s in collect-host-metrics.sh keepalive.sh; do
      local src="$APP_DIR/infra/scripts/$s"
      [[ -f "$src" ]] || continue
      sudo ln -sfn "$src" "/usr/local/bin/$s" 2>/dev/null \
        && log "sync_infra: symlinked /usr/local/bin/$s -> $src"
    done
  fi

  # Pre-create the host-metrics dir so the backend's bind-mount source
  # always exists before `docker compose up`. Without sudo we may not
  # be able to create root-owned dirs under /opt — log + continue, the
  # VM_MONITORING.md install runbook covers the one-time mkdir.
  mkdir -p /opt/sales-platform/host-metrics 2>/dev/null \
    || log "WARN: could not pre-create /opt/sales-platform/host-metrics (likely needs sudo, see VM_MONITORING.md)"

  rm -f "$tar_path"
  log "sync_infra: done"
}

# -----------------------------------------------------------------------------
# Post-deploy assertion: /api/health must report vm_metrics_available=true.
#
# Catches the kind of regression that landed on 2026-04-17: code change to
# the compose mount (or collector script) succeeded in git, image rebuild
# succeeded, deploy SSH succeeded, but `/host` was never actually mounted
# into the backend so the VM Health panel stayed dark. The healthcheck
# above only verifies HTTP responsiveness; this one verifies the metrics
# pipeline end-to-end.
#
# Tolerant:
# - Soft-warn (don't fail) if the snapshot is just slow to refresh — the
#   collector is a 2-min cron, so a freshly-restarted backend may legitimately
#   read the previous snapshot. We retry 3× with 30 s spacing, only failing
#   if vm_metrics_available stays false the whole time.
# - The /api/health response field defaults to false if the host_stats
#   call raises (see app/main.py::health), so we never hit a parse error
#   here — the field is always a literal `false` or `true`.
# -----------------------------------------------------------------------------
verify_vm_metrics_pipeline() {
  local attempts=3 wait_s=30 attempt body
  for ((attempt=1; attempt<=attempts; attempt++)); do
    body="$(curl -sf --max-time 10 http://localhost:8000/api/health 2>/dev/null || true)"
    if echo "$body" | grep -q '"vm_metrics_available": *true'; then
      log "vm_metrics pipeline OK (attempt $attempt/$attempts)"
      return 0
    fi
    if (( attempt < attempts )); then
      log "vm_metrics pipeline not yet ready (attempt $attempt/$attempts), waiting ${wait_s}s"
      sleep "$wait_s"
    fi
  done
  log "WARN: vm_metrics_available stayed false after ${attempts} attempts."
  log "WARN: response body: ${body:0:300}"
  log "WARN: check /opt/sales-platform/host-metrics/metrics.json + backend bind-mount."
  # Don't `die` here — the deploy itself is functionally fine, the VM panel
  # is just dark. Surface as WARN so the GH Actions log shows a yellow
  # signal but doesn't block traffic. If you want hard-fail, change WARN→die.
  return 1
}

# -----------------------------------------------------------------------------
# deploy <TAG>
# -----------------------------------------------------------------------------
action_deploy() {
  local tag="$1"
  local ghcr_user="${2:-}"
  validate_tag "$tag"
  cd "$APP_DIR"

  local prev
  prev="$(current_tag)"
  log "Deploy start: new=$tag, previous=$prev"
  echo "$prev" > "$APP_DIR/.prev-release"

  # Optional stdin-token login (needed for private GHCR packages)
  if [[ -n "$ghcr_user" ]]; then
    ghcr_login_from_stdin "$ghcr_user"
    # Ensure we log out even on failure
    trap 'ghcr_logout' EXIT
  fi

  # F234: read line 2 from stdin (ANTHROPIC_API_KEY) and persist to .env
  # before the rolling restart, so the backend container picks up the new
  # value when it comes back up. No-op if the line is empty (documented
  # "leave .env alone" contract from deploy.yml).
  persist_anthropic_key_from_stdin
  # Adzuna UAE-jobs fetcher creds (stdin lines 3 + 4). Read in the exact
  # order deploy.yml emits them. Same "empty = leave alone" contract.
  persist_env_var_from_stdin ADZUNA_APP_ID
  persist_env_var_from_stdin ADZUNA_APP_KEY

  # 2026-04-17 fix: sync infra files (compose YAML, helper scripts, nginx
  # config) from the tarball deploy.yml SCP'd to /tmp before invoking us.
  # MUST run before any compose command — otherwise the upcoming
  # `$COMPOSE up -d backend` reads stale yml and we ship the same
  # half-rolled state we just shipped. Idempotent + no-op without a tarball.
  sync_infra_tarball

  pre_deploy_backup "$tag"

  log "Pulling images tagged $tag"
  if ! docker pull "${GHCR_IMAGE_BACKEND}:${tag}"; then
    die "Failed to pull ${GHCR_IMAGE_BACKEND}:${tag}"
  fi
  if ! docker pull "${GHCR_IMAGE_FRONTEND}:${tag}"; then
    die "Failed to pull ${GHCR_IMAGE_FRONTEND}:${tag}"
  fi

  # Retag locally so existing compose image names (platform-backend:$RELEASE_TAG) match
  docker tag "${GHCR_IMAGE_BACKEND}:${tag}" "platform-backend:${tag}"
  docker tag "${GHCR_IMAGE_FRONTEND}:${tag}" "platform-frontend:${tag}"
  docker tag "${GHCR_IMAGE_BACKEND}:${tag}" "platform-backend:latest"
  docker tag "${GHCR_IMAGE_FRONTEND}:${tag}" "platform-frontend:latest"

  set_release_tag "$tag"

  log "Running alembic migrations"
  if ! $COMPOSE run --rm --no-deps backend alembic upgrade head; then
    log "Migration failed -- rolling back to $prev"
    set_release_tag "$prev"
    die "Migration failure"
  fi

  # F246(a) regression fix — seed the synthetic platform boards (HN
  # "Who is Hiring?", YC Work at a Startup, etc.) so Track B fetchers
  # have rows to scan. Pre-fix, ``seed_remote_companies`` was only run
  # manually post-deploy; new aggregator-style platforms shipped to
  # prod with zero boards and silently produced 0 jobs until someone
  # noticed. The seed module is fully idempotent — it does
  # check-then-insert against ``companies.name`` and the
  # ``(company_id, platform, slug)`` triple — so re-running on every
  # deploy adds only the genuinely new rows.
  #
  # Failure is non-fatal: an existing prod backend with stale seeds
  # is still functional, and a human can re-run the command manually
  # if the seed step ever stalls. We log loudly so the on-call
  # reviewer sees the gap in the deploy summary.
  log "Seeding synthetic platform boards (idempotent)"
  if ! $COMPOSE run --rm --no-deps backend python -m app.seed_remote_companies; then
    log "WARN: seed_remote_companies failed — Track B platforms (HN/YC WaaS) may have 0 boards. Run manually post-deploy: docker compose -f docker-compose.prod.yml run --rm --no-deps backend python -m app.seed_remote_companies"
  fi

  log "Rolling restart: backend"
  $COMPOSE up -d --no-deps backend

  if ! wait_for_healthy backend 90; then
    log "Backend failed to report healthy -- rolling back"
    set_release_tag "$prev"
    $COMPOSE up -d --no-deps backend
    die "Backend healthcheck timeout"
  fi

  if ! backend_http_check; then
    log "Backend HTTP probe failed -- rolling back"
    set_release_tag "$prev"
    $COMPOSE up -d --no-deps backend
    die "Backend HTTP probe failure"
  fi

  # F262: ``celery-worker-heavy`` was added to docker-compose.prod.yml
  # to host memory-greedy batch tasks (rescore_jobs, reclassify_and_
  # rescore) on a dedicated 2GB container, isolated from the everyday
  # scan worker. The first deploy with the new compose file silently
  # left the new container undefined because this list of restart
  # targets was hardcoded — adding it here so future deploys keep the
  # heavy worker alive across image rolls. ``up -d --no-deps`` creates
  # the service if it doesn't exist and recreates it on image change,
  # so the same line works for first-deploy and subsequent-deploy.
  log "Rolling restart: celery-worker, celery-worker-heavy, celery-beat, frontend"
  $COMPOSE up -d --no-deps celery-worker celery-worker-heavy celery-beat frontend

  # Force-recreate nginx AFTER frontend is up. Without --force-recreate,
  # docker skips the restart (nginx image hash unchanged) and nginx keeps
  # its cached DNS pointing at the prior frontend container's IP, causing
  # 502 Bad Gateway for ~60s until docker's DNS ages out. Recreate fixes it
  # in <3s.
  log "Recreate nginx (force) to refresh upstream DNS"
  $COMPOSE up -d --no-deps --force-recreate nginx

  # Record success
  printf '{"release":"%s","previous":"%s","deployed_at":"%s"}\n' \
    "$tag" "$prev" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$APP_DIR/.last-deploy.json"

  log "Cleanup: prune dangling + keep only last 3 tagged images"
  docker image prune -f >/dev/null 2>&1 || true
  # Drop all but the 3 most recent platform-backend / platform-frontend tags.
  # CreatedAt sort, skip first 3, rm the rest. -f in case a transient "is being
  # used by another container" error slips through.
  for repo in platform-backend platform-frontend; do
    docker images "$repo" --format '{{.CreatedAt}}\t{{.Tag}}' \
      | sort -r \
      | awk 'NR>3 && $NF != "latest" {print $NF}' \
      | while read -r stale_tag; do
          [[ -n "$stale_tag" ]] && docker rmi -f "${repo}:${stale_tag}" >/dev/null 2>&1 || true
        done
  done

  # Clean up any GHCR login we established
  [[ -n "$ghcr_user" ]] && ghcr_logout && trap - EXIT

  # 2026-04-17 fix: end-to-end probe of the VM-metrics pipeline. Doesn't
  # block the deploy on failure (return code is logged but ignored) — a
  # half-cycle stale snapshot would otherwise force a manual override
  # every time. The WARN in the log is the durable signal; deploy.yml's
  # verify job has its own assertion that DOES hard-fail.
  verify_vm_metrics_pipeline || true

  log "Deploy OK: $tag"
}

# -----------------------------------------------------------------------------
# rollback <TAG>
# -----------------------------------------------------------------------------
action_rollback() {
  local tag="$1"
  local ghcr_user="${2:-}"
  validate_tag "$tag"
  cd "$APP_DIR"

  # Optional stdin-token login in case the target tag isn't cached locally
  if [[ -n "$ghcr_user" ]]; then
    ghcr_login_from_stdin "$ghcr_user"
    trap 'ghcr_logout' EXIT
  fi

  # F234: rollback path also reads the ANTHROPIC_API_KEY line so a
  # rollback re-run with a fresh Secret value rotates the key on the
  # way back. No-op when the line is empty.
  persist_anthropic_key_from_stdin

  # Check the image tag exists locally
  if ! docker image inspect "platform-backend:${tag}" >/dev/null 2>&1; then
    log "Image platform-backend:${tag} not local; pulling from GHCR"
    docker pull "${GHCR_IMAGE_BACKEND}:${tag}" || die "Cannot pull backend:${tag}"
    docker tag "${GHCR_IMAGE_BACKEND}:${tag}" "platform-backend:${tag}"
  fi
  if ! docker image inspect "platform-frontend:${tag}" >/dev/null 2>&1; then
    docker pull "${GHCR_IMAGE_FRONTEND}:${tag}" || die "Cannot pull frontend:${tag}"
    docker tag "${GHCR_IMAGE_FRONTEND}:${tag}" "platform-frontend:${tag}"
  fi

  local prev
  prev="$(current_tag)"
  log "Rollback: $prev -> $tag"
  set_release_tag "$tag"

  $COMPOSE up -d --no-deps backend
  wait_for_healthy backend 60 || log "WARN: backend not yet healthy post-rollback"
  $COMPOSE up -d --no-deps celery-worker celery-beat frontend
  # See comment in action_deploy — nginx must be recreated to drop stale DNS
  $COMPOSE up -d --no-deps --force-recreate nginx

  printf '{"release":"%s","previous":"%s","deployed_at":"%s","kind":"rollback"}\n' \
    "$tag" "$prev" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$APP_DIR/.last-deploy.json"

  [[ -n "$ghcr_user" ]] && ghcr_logout && trap - EXIT

  log "Rollback OK: $tag"
}

# -----------------------------------------------------------------------------
# stage-infra
#
# Reads a tarball from stdin and writes it to /tmp/sales-platform-infra.tgz
# so the next `deploy` verb can pick it up via sync_infra_tarball. Split out
# from `deploy` so the GitHub Actions deploy job can ship the tarball over
# the existing forced-command SSH channel without needing a separate scp
# subsystem (which the deploy user's authorized_keys may not permit).
#
# Validation: cap the input at 10 MiB so a stuck `cat` upstream can't
# fill /tmp. The real allow-list / path-traversal validation lives in
# sync_infra_tarball — this verb just receives bytes.
# -----------------------------------------------------------------------------
action_stage_infra() {
  local out="/tmp/sales-platform-infra.tgz"
  local tmp="${out}.partial.$$"
  local max_bytes=$((10 * 1024 * 1024))   # 10 MiB

  # Stream stdin to a tmp file with a hard size cap. `head -c` returns 0
  # at EOF or after capping, so we can't distinguish the two — check the
  # resulting size separately and reject if we hit the cap exactly (the
  # input was probably truncated).
  if ! head -c "$max_bytes" > "$tmp"; then
    rm -f "$tmp"
    die "stage-infra: failed to read tarball from stdin"
  fi
  local size
  size="$(stat -c '%s' "$tmp" 2>/dev/null || echo 0)"
  if (( size == 0 )); then
    rm -f "$tmp"
    die "stage-infra: tarball is empty (stdin closed before any bytes arrived)"
  fi
  if (( size == max_bytes )); then
    rm -f "$tmp"
    die "stage-infra: tarball hit the ${max_bytes}-byte cap; refusing as likely truncated"
  fi

  # Quick sanity check: gzip header (magic 1f 8b). Tarballs not built by
  # this repo's `tar czf` would fail downstream too, but failing here
  # gives a clearer error.
  local magic
  magic="$(head -c 2 "$tmp" | xxd -p 2>/dev/null || true)"
  if [[ "$magic" != "1f8b" ]]; then
    rm -f "$tmp"
    die "stage-infra: stdin doesn't look like gzip (magic=$magic), refusing"
  fi

  mv -f "$tmp" "$out"
  log "stage-infra: staged ${size}-byte tarball at $out (will be consumed by next deploy)"
}

# -----------------------------------------------------------------------------
# status
# -----------------------------------------------------------------------------
action_status() {
  cd "$APP_DIR"
  echo "=== Current release ==="
  current_tag
  echo ""
  echo "=== Last deploy ==="
  cat "$APP_DIR/.last-deploy.json" 2>/dev/null || echo "(none)"
  echo ""
  echo "=== Containers ==="
  $COMPOSE ps --format "table {{.Name}}\t{{.Status}}"
}

# -----------------------------------------------------------------------------
# Dispatch
# -----------------------------------------------------------------------------
# SSH_ORIGINAL_COMMAND contains whatever the client sent. If the script is run
# directly (not via forced-command SSH), fall back to $@.
CMD="${SSH_ORIGINAL_COMMAND:-$*}"
# shellcheck disable=SC2206
ARGS=($CMD)
ACTION="${ARGS[0]:-}"
ARG1="${ARGS[1]:-}"
ARG2="${ARGS[2]:-}"

case "$ACTION" in
  deploy)      action_deploy "$ARG1" "$ARG2" ;;
  rollback)    action_rollback "$ARG1" "$ARG2" ;;
  status)      action_status ;;
  stage-infra) action_stage_infra ;;
  *)
    echo "Usage: deploy <TAG> [GHCR_USER] | rollback <TAG> [GHCR_USER] | status | stage-infra" >&2
    echo "Got: '$CMD'" >&2
    exit 1
    ;;
esac
