# Production Deployment Plan — Sales Platform

> **Goal:** PR merged to `main` → CI green → one-click (or auto) deploy to the Oracle VM → live at `salesplatform.reventlabs.com` in < 3 min, with rollback in < 30 sec.
>
> **Constraint:** Zero secret leakage, minimal blast radius if a key is stolen, fail-safe on bad deploys.

---

## 1. Current state (audit)

| Piece | Status | Notes |
|---|---|---|
| Repo workflows | `platform/.github/workflows/deploy.yml` exists but is at **wrong path** — GH reads only `.github/workflows/` at repo root | Currently not running |
| Deploy mode | Manual: `scp` + `docker cp` (what I've been doing) | Works but no audit trail, no gating |
| Existing CI workflow | Backend + frontend + docker build | Fine; just needs relocating |
| Ansible playbooks | `platform/ansible/` exists with `deploy.yml`, `rollback.yml` | Unused by CI today; used only by `scripts/deploy.sh` on VM |
| Prod compose | Parameterised with `${RELEASE_TAG:-latest}` | Image-tag deploys are already supported |
| SSH access | One key `Sarthak-Betaque` → `ubuntu` user (sudo + docker) | Single shared key; high blast radius if stolen |
| Firewall | ufw: only port 22 open (80/443 via Cloudflare Tunnel) | Good; keep as is |
| `.env` on VM | `mode 0600`, owner `ubuntu`, only source of truth for secrets | Good; **must never be synced from CI** |

---

## 2. Threat model

| Threat | Impact | Control |
|---|---|---|
| Deploy SSH key stolen from GH Secrets | Attacker SSHes in as `ubuntu`, full sudo, can read `.env`, tamper DB | Rotate key, scope to dedicated `deploy` user (no sudo), `from=` + `restrict` in authorized_keys, GH Environment approval gate |
| GitHub account of a dev with push-to-main access compromised | Malicious commit → auto-deploy | Branch protection on `main` + required PR review + `production` environment approval |
| Supply-chain injection (npm/pip/docker base image) | Malicious build artifact reaches prod | Pinned versions, Dependabot, optional cosign image signing |
| Prod `.env` exfiltration | Attacker gets `JWT_SECRET`, `ANTHROPIC_API_KEY`, Postgres password | Mode 0600, owner non-root, offsite encrypted backup (1Password), never in repo/CI logs |
| Bad migration / bad code reaches prod | Downtime, data loss | Pre-deploy DB backup (already in workflow), health-check + auto-rollback, keep last 3 image tags |
| CI logs leak secrets | Credentials in public logs | `::add-mask::` for any echoed secret; never `echo $KEY`; use `mask_logs` patterns |
| Registry compromise (GHCR token) | Poisoned image pulled to prod | Use short-lived OIDC token (no long-lived PAT), pin images by SHA digest, not tag |

---

## 3. Recommended architecture

**Option A (current): rsync full tree, build on VM.**
Pro: simple, no registry needed. Con: slow (ARM builds ≈ 5 min), CPU-spikes the free-tier VM on every deploy.

**Option B (recommended): build images in CI → push to GHCR → VM pulls + swaps tag.**
Pro: fast deploys (~90 sec), immutable releases, instant rollback (change tag), no VM CPU burn.
Con: requires GHCR auth + slight complexity.

> **Recommendation: Option B.** The VM already supports `RELEASE_TAG`, so half the work is done.

### 3.1 Diagram

```
┌──────── GitHub ────────┐        ┌──── Oracle VM ────┐        ┌─── Cloudflare ───┐
│                        │        │                   │        │                  │
│  PR → main             │        │   /opt/sales-     │        │                  │
│  CI (test+typecheck+   │        │        platform/  │        │                  │
│      docker build)     │        │   .env (local!)   │        │                  │
│                        │        │                   │        │                  │
│  CD (build images,     │        │   docker compose  │        │                  │
│      push to GHCR)     │        │   pulls GHCR img  │        │                  │
│                        │ SSH+   │   with new tag    │ Tunnel │  salesplatform.  │
│  ┌──────────────────┐  │ OIDC   │                   │ (out)  │  reventlabs.com  │
│  │ prod environment │──┼───────►│  deploy user      │◄───────│                  │
│  │ (approval gate)  │  │        │  (docker grp only)│        │                  │
│  └──────────────────┘  │        │  no sudo          │        │                  │
└────────────────────────┘        └───────────────────┘        └──────────────────┘
```

---

## 4. Security controls

### 4.1 VM: dedicated deploy user

```bash
# On the VM as ubuntu:
sudo useradd -m -s /bin/bash -G docker deploy       # docker group, no sudo
sudo mkdir -p /home/deploy/.ssh
sudo cp /opt/sales-platform/.env /home/deploy/.env.readonly   # optional
sudo chown -R deploy:deploy /opt/sales-platform /home/deploy/.ssh
sudo chmod 700 /home/deploy/.ssh
```

Result: deploy key cannot `sudo`, cannot read `/root`, cannot edit `/etc`. If stolen → attacker can tamper with `/opt/sales-platform` only (bad, but recoverable).

### 4.2 VM: restricted SSH key

Add the CI key to `/home/deploy/.ssh/authorized_keys` with restrictions:

```
restrict,command="/opt/sales-platform/scripts/ci-deploy.sh",from="140.82.112.0/20,143.55.64.0/20,192.30.252.0/22,185.199.108.0/22,2606:50c0::/32" ssh-ed25519 AAA... github-ci-deploy
```

- `restrict` disables port-forwarding, agent, X11, pty
- `command=` forces any `ssh` session to run only our deploy script (ignores any user-sent command)
- `from=` limits to GitHub Actions IP ranges (https://api.github.com/meta)

Human admins (me / you) keep their own keys on the `ubuntu` user, unchanged.

### 4.3 GitHub: Environment + Branch protection

**Environment `production`:**
- Required reviewer: the owner (at least one)
- Wait timer: 2 min (lets you cancel an unwanted auto-deploy)
- Secrets scoped ONLY to this environment:
  - `DEPLOY_SSH_KEY` (the ed25519 private key)
  - `DEPLOY_HOST` = *(VM public IP — not committed; set in GH Environment only)*
  - `DEPLOY_USER` = `deploy`
- No repo-wide secrets for anything touching prod

**Branch protection on `main`:**
- Require pull request before merging
- Require 1 approving review (can be self for solo dev — still forces PR flow)
- Require status checks: `CI / backend`, `CI / frontend`, `CI / docker`
- Require branches up to date
- Restrict pushes to `main` (only via PR merge)
- Disallow force-push on `main`

### 4.4 GHCR: OIDC, not PAT

Workflow auth:
```yaml
permissions:
  contents: read
  packages: write
  id-token: write
```

Use `docker/login-action@v3` with `password: ${{ secrets.GITHUB_TOKEN }}` — this token is minted per-run, expires at job end. **No long-lived PAT anywhere.**

Image names:
- `ghcr.io/betaque-team/sales-platform-backend:sha-<short>`
- `ghcr.io/betaque-team/sales-platform-frontend:sha-<short>`
- Plus moving tag `latest` (for emergency manual pull)

On the VM, give the `deploy` user a read-only GHCR PAT (stored in `/home/deploy/.docker/config.json`, mode 600). Rotate every 90 days.

### 4.5 Secrets handling

| Secret | Storage | Rotated | Used by |
|---|---|---|---|
| `DEPLOY_SSH_KEY` (ed25519) | GH Environment `production` | 90 days | Deploy workflow only |
| `DEPLOY_HOST`, `DEPLOY_USER` | GH Environment `production` | n/a | Deploy workflow |
| `GHCR_READ_TOKEN` (on VM) | `/home/deploy/.docker/config.json`, mode 600 | 90 days | `docker pull` on VM |
| Prod `.env` (DB password, JWT_SECRET, ANTHROPIC, etc.) | `/opt/sales-platform/.env`, mode 600 | as needed | docker-compose on VM |
| Offsite `.env` backup | 1Password / encrypted personal store | on change | Humans only |

**Rules:**
- CI workflow never reads or writes prod `.env`
- CI logs scrub any env var echo with `::add-mask::`
- Never `cat` or `grep` `.env` in a workflow step

### 4.6 Deploy safety rails

1. **DB backup** — the nightly Celery backup (03:00 UTC, keeps the last 3) is the only backup stream; pre-deploy dumps were retired on 2026-09-11 after 53 of them (70 GB) filled the disk. Before a risky deploy, take one on demand: `POST /api/v1/monitoring/backup` (Monitoring → Backup now).
2. **Migrations first** — `alembic upgrade head` runs in a one-shot container before the swap. Failure = hard stop, no swap.
3. **Rolling restart** — backend first (health-check 30 s), then celery + frontend + nginx.
4. **Health-check** — `curl -fsS https://salesplatform.reventlabs.com/api/v1/monitoring` returns 2xx/401 within 60 s, else auto-rollback.
5. **Auto-rollback** — if health-check fails: set `RELEASE_TAG` to previous, `docker compose up -d`. Alert on failure.
6. **Image retention** — keep last 3 tags on the VM; keep last 30 in GHCR (GC older).

### 4.7 Observability

- Every deploy writes `/opt/sales-platform/.last-deploy.json` (tag, SHA, timestamp, duration)
- GH Actions summary table (tag, SHA, commit message, duration, link to logs)
- Optional (Phase 4): Slack/Discord webhook on success + failure
- Optional (Phase 4): Sentry DSN in `.env` for runtime errors

---

## 5. Workflow design

Two workflows at **repo root** `.github/workflows/`:

### 5.1 `ci.yml` — runs on every PR and push
- `backend-tests` (pytest + alembic upgrade against ephemeral postgres)
- `frontend-build` (tsc + vite build)
- `docker-build` (verify both images build)
- Gates merges to main

### 5.2 `deploy.yml` — runs only on push to main (after CI) or manual
- `environment: production` (approval gate kicks in here)
- Job 1 — **build & push**:
  - Build `platform-backend` and `platform-frontend` images
  - Tag `sha-<short>` + `latest`
  - Push to GHCR via GITHUB_TOKEN (OIDC)
- Job 2 — **deploy** (needs job 1):
  - SSH to VM as `deploy` user
  - VM runs `ci-deploy.sh <tag>`:
    - Pre-deploy DB backup
    - `docker pull` new images
    - `docker compose run --rm backend alembic upgrade head`
    - `export RELEASE_TAG=<tag>; docker compose up -d backend` → wait for healthy
    - `docker compose up -d celery-worker celery-beat frontend nginx`
    - `curl` health-check; on fail → roll back tag, alert
    - Write `.last-deploy.json`, prune old images
- Job 3 — **summary** (always):
  - Write deploy summary to GH Step Summary
  - Optional webhook

### 5.3 `rollback.yml` — `workflow_dispatch` only
- Input: `target_tag` (defaults to prior release from `.last-deploy.json`)
- Requires same `production` environment approval
- SSH → set `RELEASE_TAG=<target>` → `docker compose up -d`
- Does NOT restore DB unless `restore_db=true` input is passed (dangerous, requires extra confirmation)

---

## 6. Implementation phases

### Phase 0 — Today: close manual deploys cleanly
- ✅ Ship fix-branch fixes manually (done)
- ☐ Merge `fix/regression-findings` → main via PR (audit trail)

### Phase 1 — Foundation (30 min)
1. Create `deploy` user on VM, add to `docker` group, own `/opt/sales-platform`
2. Generate dedicated CI ed25519 keypair on your Mac:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/sales-platform-ci -C "github-ci-deploy"
   ```
3. Add public key to `/home/deploy/.ssh/authorized_keys` on VM with `restrict,command=,from=`
4. Store private key as GH Environment secret `DEPLOY_SSH_KEY` (env = `production`)
5. Configure `production` environment: required reviewer + 2-min wait timer
6. Move existing `platform/.github/workflows/*` → `/.github/workflows/*` (fix paths so they actually run)
7. Enable branch protection on `main`

### Phase 2 — Image-based CD (1 hour)
1. Rewrite `deploy.yml` for GHCR build-and-push
2. Write `/opt/sales-platform/scripts/ci-deploy.sh` (VM-side, called by CI's forced-command SSH)
3. Add GHCR read-only PAT on VM for `deploy` user
4. Dry-run via `workflow_dispatch` on a test branch
5. First real deploy from main

### Phase 3 — Safety rails (30 min)
1. Add `rollback.yml` workflow with manual dispatch
2. Add health-check → auto-rollback logic in `ci-deploy.sh`
3. Document runbook: "deploy failed, what do I do?" in `DEPLOY.md`

### Phase 4 — Observability (optional, later)
1. Slack/Discord webhook
2. Sentry DSN
3. Cosign image signing
4. Dependabot for workflow actions + backend/frontend deps

---

## 7. What to migrate from the existing workflow

The existing `platform/.github/workflows/deploy.yml` I inspected is close to right but has four fixable issues:

| Issue | Fix |
|---|---|
| Located at `platform/.github/workflows/` — GH ignores it | Move to repo-root `.github/workflows/` |
| Uses `./backend` / `./frontend` paths assuming cwd is `platform/` | Use `platform/backend` etc. or `working-directory: platform` |
| SSHes as `ubuntu` (sudo, docker, all groups) | Switch to dedicated `deploy` user |
| Builds on VM via rsync (slow, burns ARM CPU) | Build in CI, push to GHCR, pull on VM |
| No `environment: production` gate | Add environment → gets the approval + secret scoping for free |

---

## 8. Open questions for you

Before I start implementing, confirm these decisions:

1. **Approval gate:** auto-deploy on merge, or require a one-click approval in GH every time? (I recommend a 2-min wait timer — auto-proceeds unless you hit Cancel.)
2. **GHCR vs rsync:** do you want the registry-based approach (recommended), or keep rsync + build-on-VM for simplicity?
3. **Dedicated deploy user:** OK to create `deploy` user on VM with no sudo? (Recommended, but requires you run two commands on the VM.)
4. **Branch protection:** OK to enforce "PRs required to push to main"? (Recommended but changes your workflow — no more `git push origin main` directly.)
5. **Rollback scope:** is "roll back the app images to prior tag" sufficient, or do you also need DB rollback on a bad deploy? (DB rollback is destructive — I recommend opt-in only via `rollback.yml` with an extra flag.)

Reply with ✅ / ❌ on each and I'll start Phase 1.
