# Releases

Chronological log of production releases to `salesplatform.reventlabs.com`.
Newest at the top. Each entry is one merge to `main` that triggered a deploy
via [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml).

**How this file is maintained.** The deploy workflow appends a stub entry
automatically on every successful merge-to-main run (see the `append-release`
step in `deploy.yml`). The stub contains: short SHA, tag, UTC timestamp, and
the commit subject. A human can then edit the stub to add the "why it
matters" line — keep it user-facing, one sentence, no implementation details.

**Format per entry:**

```
## YYYY-MM-DD · <short-sha> · <tag>
<one-line commit subject>

<optional one-sentence user-facing summary>
```

The one-line commit subject is the machine-written bit; the user-facing
summary is the human edit. Entries with only the subject (no summary) are
still-to-be-annotated — fine to leave as-is for minor fixes.

For per-feature deep-dives, the `docs/releases/` subdirectory holds the
long-form writeups (one `.md` per notable round). This file is the index.

---

<!-- RELEASES_LOG_START -->

## 2026-09-11 · 13fa0c9 · sha-13fa0c9
Merge: answer quality (F403)


## 2026-09-11 · e7ff396 · sha-e7ff396
Merge: laptop backup job outside the checkout


## 2026-09-11 · 721c217 · sha-721c217
Merge: laptop backup pull


## 2026-09-11 · c6b6e11 · sha-c6b6e11
Merge: a board whose fetch throws keeps its ScanLog (F402, tester)


## 2026-09-11 · 596352b · sha-596352b
Merge: retire pre-deploy dumps, nightly backup keeps 3


## 2026-09-11 · 7dad871 · sha-7dad871
Merge: sweeper handles interrupted dry runs


## 2026-09-11 · 5110bbf · sha-5110bbf
Merge: task registry fix


## 2026-09-11 · be645ce · sha-be645ce
Merge: draft task observability


## 2026-09-11 · f7a5edb · sha-f7a5edb
Merge: periodic task limits + revoke (F401)


## 2026-09-11 · c05f0a9 · sha-c05f0a9
Merge: worker queue hygiene (F401)


## 2026-09-11 · 06493d9 · sha-06493d9
Merge: worker diagnostics + tester round 2 (F399, F400)


## 2026-09-11 · 4487b53 · sha-4487b53
Merge: tester findings 2026-09-11 (F398)


## 2026-09-11 · 960e140 · sha-960e140
Merge: own-words rule for drafts, third worker child (F396 fix, F397)


## 2026-09-11 · 0315f3b · sha-0315f3b
Merge: Opus 5 everywhere + drafted answers for approval (F395, F396)


## 2026-09-11 · 317cb8f · sha-317cb8f
Merge: per-site auto-apply guide + answer-matching correctness (F393, F394)


## 2026-09-11 · 00194f9 · sha-00194f9
Merge: ATS batch 2 — Pinpoint, Jobvite, Hireology, Dover, Gem auto-apply; Zoho scan; YC/JOIN/Polymer/CareerPlug refusals


## 2026-09-11 · f635a17 · sha-f635a17
fix(gate): an answer that isn't one of the form's options is a gap; don't retry unplaceable fields


## 2026-09-11 · a9e24a4 · sha-a9e24a4
fix(ui): pasting a job you already have opens it instead of erroring


## 2026-09-11 · 390f818 · sha-390f818
feat(ui): the auto-apply home — paste, "Needs you" with reasons, today's cap, recent


## 2026-09-11 · 5c02a25 · sha-5c02a25
fix(own-link): re-point a same-platform twin whose id predates namespacing


## 2026-09-11 · a374425 · sha-a374425
feat(own-link): a repost's resolution runs in the worker; the paste polls


## 2026-09-11 · e25bf10 · sha-e25bf10
fix(own-link): match the company by slug too; surface a failed save


## 2026-09-11 · 8b355ee · sha-8b355ee
test: probe fixture uses the normalised slug


## 2026-09-11 · f858a5e · sha-f858a5e
feat(ats): JazzHR (scan + extract, walled) and Teamtailor (scan, extract, submit); faster paste flow


## 2026-09-11 · 912fa70 · sha-912fa70
feat(ats): Breezy HR — scan, extract, submit


## 2026-09-11 · dda5fce · sha-dda5fce
Merge: bound paste-time resolution; canonical URLs for probe hits


## 2026-09-11 · 12c26c8 · sha-12c26c8
Merge: company + title resolver, and Himalayas links in Add-your-own-link


## 2026-09-11 · bf2f8be · sha-bf2f8be
Merge: aggregator resolver circuit-breaker


## 2026-09-11 · c446e8b · sha-c446e8b
Merge: resolve aggregator reposts to the employer's real form


## 2026-09-11 · bfec046 · sha-bfec046
Merge: give the paste-a-link box its own row


## 2026-09-11 · 9544719 · sha-9544719
Merge: load the whole model registry in the worker — no submission had ever run


## 2026-09-11 · 0e1dac4 · sha-0e1dac4
Merge: bring your own link, and a standing instruction for the sweep


## 2026-09-11 · 7304118 · sha-7304118
Merge: a guessed template cached before F357 no longer reads as extracted


## 2026-09-11 · 5e13e13 · sha-5e13e13
Merge: a walled form is never safe to auto-submit


## 2026-09-11 · a2cb3e3 · sha-a2cb3e3
Merge: stop demanding an ATS login where the server drives a public form


## 2026-09-11 · b9d2f3a · sha-b9d2f3a
Merge: Ashby server-side submitter; name the wall on SR/BambooHR/Lever


## 2026-09-11 · 5d1507c · sha-5d1507c
feat(apply): BambooHR extraction over plain HTTP


## 2026-09-11 · 97148ae · sha-97148ae
feat(apply): Ashby extraction via the rendered page; fix two gate bugs


## 2026-09-10 · 0e9fab4 · sha-0e9fab4
feat(apply): Workable extraction + submitter — third auto-apply platform


## 2026-09-10 · 1f347b6 · sha-1f347b6
fix(apply): alternative groups vanished on cached reads; fast-fail DataDome


## 2026-09-10 · 0791226 · sha-0791226
fix(apply): review screen overstated what was blocking


## 2026-09-10 · 9b3c215 · sha-9b3c215
fix(apply): the feature was unreachable from the UI


## 2026-09-10 · a133952 · sha-a133952
fix(tests): route test enumerated app.routes; red on main, green locally


## 2026-09-10 · 9e22c25 · sha-9e22c25
Merge: server-side auto-apply — gate, submitters, review queue


## 2026-09-10 · 0c2cdfe · sha-0c2cdfe
Merge: job-apply skill learnings from the 2026-09-10 run


## 2026-09-10 · ceb9f11 · sha-ceb9f11
Merge: release-log step no longer fails a green deploy


## 2026-09-10 · 73ccab5 · sha-73ccab5
fix(jobs): reopening a rejected job 500'd on the F316 dedupe index


## 2026-09-10 · 3c4fe78 · sha-3c4fe78
Merge: narrow remote_policy using the job body


## 2026-09-10 · 7752e95 · sha-7752e95
fix(pipeline): label legacy prepared_answers rows in the card panel


## 2026-09-10 · 0a0e673 · sha-0a0e673
Merge: render submitted answers in pipeline card panel


## 2026-09-10 · 6bad990 · sha-6bad990
Merge: submitted answers on pipeline card drill-down


## 2026-09-09 · 7ead145 · sha-7ead145
Merge: harden job-apply against live Greenhouse testing


## 2026-09-09 · 59cd178 · sha-59cd178
Merge: route enumeration via OpenAPI (FastAPI >=0.141 lazy includes)


## 2026-09-09 · aa98809 · sha-aa98809
Merge: job-apply skill + POST /applications/record


## 2026-08-18 · 3bed209 · sha-3bed209
Merge: full-component render test for Relevant→All Jobs highlight


## 2026-08-18 · 9d3baba · sha-9d3baba
Merge: scope vitest to src/ (unbreak CI unit-test step)


## 2026-08-10 · fd1afe8 · sha-fd1afe8
Merge: add RemoteDXB fetcher (UAE remote/hybrid, real employers)


## 2026-08-10 · f741598 · sha-f741598
fix(jobs): resolve already-scanned jobs in submit-link (feedback 821bb39d)


## 2026-08-09 · 15cf7b6 · sha-15cf7b6
Merge: add missing platforms (incl. Jobsora) to Jobs Platform dropdown


## 2026-08-09 · a0e6cc2 · sha-a0e6cc2
Merge: add Jobsora fetcher (UAE aggregator source)


## 2026-08-05 · 04f040e · sha-04f040e
Merge: classify ~87k unknown remote jobs (bare country + remote)


## 2026-08-05 · e7273b4 · sha-e7273b4
Merge: propagate Adzuna secrets to VM .env (activate UAE fetcher)


## 2026-08-05 · f9d618c · sha-f9d618c
Merge: detect bare 'UAE' location (more UAE jobs)


## 2026-08-05 · a9b27e1 · sha-a9b27e1
Merge: in-app login notice banner (re-upload comms for 650514ad)


## 2026-08-05 · 95b9082 · sha-95b9082
Merge: stop CDN caching feedback attachments (security, auth bypass)


## 2026-08-05 · aedfd1f · sha-aedfd1f
Merge: persist uploaded files across deploys (critical fix, feedback 650514ad)


## 2026-08-05 · aa3ae51 · sha-aa3ae51
Merge: UAE hybrid detection from description body (UAE-only)


## 2026-08-05 · 391b582 · sha-391b582
Merge: All-Jobs applied marker (14d00e33) + UAE hybrid capture & filter preset


## 2026-07-31 · 94b40cc · sha-94b40cc
fix(ai): F357 — retired model ID broke every AI feature; centralize + fix insights SQL


## 2026-07-31 · 2fa1bc9 · sha-2fa1bc9
fix(celery): F356 — register beat tasks that were silently unregistered


## 2026-07-31 · 8bfc3e3 · sha-8bfc3e3
fix(deploy): point GHCR pull at sk-rl after org rename


## 2026-07-26 · ccff960 · sha-ccff960
Merge feat/contact-harvesting: F354 — recruiter email sources


## 2026-07-26 · 16b9f63 · sha-16b9f63
fix(ci): rebase-retry the release-log push — stop false deploy failures


## 2026-07-26 · 4a24988 · sha-4a24988
Merge fix/sheet-company-attribution: F353


## 2026-07-24 · a1447b0 · sha-a1447b0
Merge feat/google-sheet-ingestion: F351 — team Google Sheets as scan source


## 2026-07-24 · c31e60f · sha-c31e60f
fix(ci): SSH keepalives on deploy/rollback — backup outlives idle timeout


## 2026-06-24 · 5b3feb4 · sha-5b3feb4
Merge fix/adzuna-migration-unblock-deploy: restore broken prod deploy pipeline


## 2026-04-29 · cc908b5 · sha-cc908b5
Merge feat/f275-companies-name-trigram into main


## 2026-04-29 · ffb8f66 · sha-ffb8f66
Merge feat/f274-jobs-title-trigram-index into main


## 2026-04-29 · c3ee2df · sha-c3ee2df
Merge feat/f273-uvicorn-multi-worker into main


## 2026-04-29 · b8a0186 · sha-b8a0186
Merge fix/f272d-prune-uuid-max-fix into main


## 2026-04-29 · 0aa0cc1 · sha-0aa0cc1
Merge fix/f272c-import-scanlog into main


## 2026-04-29 · 331896e · sha-331896e
Merge feat/f272-scan-logs-retention into main


## 2026-04-29 · b3f1ca7 · sha-b3f1ca7
Merge fix/f271-stable-pagination-tiebreaker into main


## 2026-04-29 · e091e86 · sha-e091e86
Merge fix/f269-classifier-negative-parity into main


## 2026-04-29 · 8087430 · sha-8087430
Merge fix/f268-strict-admin-schemas into main


## 2026-04-29 · 7c37c5d · sha-7c37c5d
Merge fix/f266-hn-error-msg-and-pipeline-app-count into main


## 2026-04-29 · 2d58204 · sha-2d58204
Merge fix/migration-collision-and-jsonb-binding: unblock alembic + JSONB filter


## 2026-04-23 · 1a7b853 · sha-1a7b853
chore(ui): rename "Claude Routine" to "Apply Routine" in user-visible copy


## 2026-04-23 · 98c529d · sha-98c529d
Merge feat/routine-apply-improvements: phase 1 + 2 routine-apply improvements


## 2026-04-22 · bbe5028 · sha-bbe5028
fix(humanizer): style_match_pass corpus-size gate compared wrong length


## 2026-04-22 · 85fd7a4 · sha-85fd7a4
Merge feat/claude-routine-apply: Claude Routine Apply (v6)


## 2026-04-22 · 980ae9c · sha-980ae9c
docs(regression-report): Round 5A test plan for Track B features


## 2026-04-22 · 14f2f28 · sha-14f2f28
Merge branch 'fix/regression-findings' into main


## 2026-04-22 · 2a10d9e · sha-2a10d9e
Merge feat/admin-profile-docs-vault: admin-only KYC docs vault


## 2026-04-19 · 318fd4a · sha-318fd4a
Merge fix/feedback-f242-f243-f244: resolve F242, F243, F244


## 2026-04-18 · c19dd67 · sha-c19dd67
Merge feat/cross-platform-job-dedup: normalized-title dedup across ATSes


## 2026-04-18 · 19a842e · sha-19a842e
Merge test/phase-a-careers-url-coverage: 4 regression tests for Phase A


## 2026-04-18 · c4a2a29 · sha-c4a2a29
Merge chore/schedule-fingerprint-beat: auto-run the fingerprint task daily


## 2026-04-18 · 39224a2 · sha-39224a2
Merge feat/company-careers-url-phase-a: store per-company careers URL for future fallback


## 2026-04-17 · 93f27e8 · sha-93f27e8
Merge feat/bulk-fingerprint-companies: reverse-discovery via Company.website scraping


## 2026-04-17 · affbd5a · sha-affbd5a
Merge feat/workday-fetcher-and-ats-fingerprint: 7k enterprise jobs + discovery foundation


## 2026-04-17 · eaf6191 · sha-eaf6191
Merge fix/refresh-fetcher-probe-slugs: fetcher survey + live-API tests


## 2026-04-17 · 15cfda7 · sha-15cfda7
Merge feat/manual-link-review-priority-applied: ship platform/scripts/ci-deploy.sh + close out F231-F234


## 2026-04-17 · fea6b84 · sha-fea6b84
Merge release/v0.1.1


# v0.1.1 — 2026-04-17

Version bump consolidating everything merged to `main` since v0.1.0
(initial tag). Backend + frontend both move to `0.1.1`. No breaking
changes, no migrations beyond those already shipped with the features
below; safe to roll forward.

**What users see:**

* **Cover letters now use Claude Opus 4.7** (was Sonnet 4). Noticeably
  more specific to the job posting and the candidate's résumé phrasing;
  fewer generic "excited to leverage" openings. Resume customization
  and interview prep stay on Sonnet 4 — higher volume, different
  quality needs.
* **Answer Book fills itself from your résumé.** Uploading or switching
  an active résumé auto-populates email, phone, LinkedIn, GitHub into
  the Answer Book. The "Import from Resume" button is gone — it was
  redundant.
* **LinkedIn is now a credential slot.** Each résumé can carry its
  LinkedIn profile URL (and optional login) alongside the ATS creds.
* **Submit link, review queue priority, Applied action** — three sales
  workflow features from earlier this sprint are now live end-to-end.
  Paste an ATS URL to import one job; review queue orders by
  today/yesterday/older + best resume fit; `P` on the review card marks
  a job applied with an immutable snapshot of the resume text used.
* **Skill Gaps page reflects current demand** instead of the oldest 500
  jobs in the DB. Missing-skills list now shifts with real hiring
  signal.
* **`GET /api/v1/companies/enrichment-coverage`** (admin) — visibility
  into how many companies have been enriched, what's pending, and top
  recent errors.

**What changed under the hood (operator-facing):**

* **Discovery actually adds boards now.** Beat schedule calls
  `discover_and_add_boards` instead of `run_discovery`; previously
  `discovered_companies` filled up but nothing got promoted to
  `company_ats_boards`. Cap of 200 promotions per run prevents a
  Greenhouse-sitemap flood; stale-cull backstops any dead slugs.
* **Enrichment covers the long tail.** The batch task no longer hard-
  filters on `is_target=True` — any company with an active ATS board
  is eligible, with `is_target DESC` preserving priority ordering.
  786-company corpus converges in ~16h at the default 50/hour cap.
* **ANTHROPIC_API_KEY leak defense.** `SecretStr` at the config layer,
  log-scrubbing filter at the root logger + Celery worker, extended
  pre-commit regex, `.env` auto-write from GH Secrets via ci-deploy.sh
  stdin contract.
* **Backend CI unblocked.** Alembic migrations pass on a fresh DB
  (dropped the FK to the un-migrated `ai_customization_logs` table);
  new `test_smoke.py` runs 10 regression guards on SecretStr + the
  scrubber.
* **F228 fix.** `GET /applications` accepts `?submission_source=`
  (the column was response-only before).
* **Release log automation.** `docs/RELEASES.md` now gets a stub
  entry prepended on every green deploy (see `append-release` job
  in `.github/workflows/deploy.yml`).

**Fix rollup below** (chronological, newest first).

## 2026-04-17 · b5d6f70 · sha-b5d6f70
Merge Round 2: discovery auto-add + enrichment long-tail coverage


## 2026-04-17 · 9a3bdd1 · sha-9a3bdd1
Merge Round 1 QoL: Opus 4.7 cover letter + answer-book auto + LinkedIn cred + releases

## 2026-04-17 · bf06008 · auto
Merge fix/skill-gaps-order-by-freshness: pin sample to newest 500 jobs

Skill-gaps page stopped silently reporting stale market demand — samples
now reflect the 500 most recently ingested jobs instead of the oldest.

## 2026-04-17 · 9b25f2f · auto
Merge fix/ci-pytest-ignore-live-integration: unblock backend test job

Backend CI test job goes green; added 10 real smoke tests as regression
guards on the SecretStr + log-scrubber leak defenses.

## 2026-04-17 · dc71fc0 · auto
Merge: ANTHROPIC_API_KEY leak defense (SecretStr + log scrubber + hook)

Three-layer protection so the Anthropic key can never reach logs,
commit messages, or stringified settings dumps.

## 2026-04-17 · 8d3d097 · auto
Merge fix/f228-applications-submission-source-filter

`GET /applications` now honours `?submission_source=…`; new "Source"
dropdown on the Applications page.

## 2026-04-17 · aecb3ca · auto
Merge deploy.yml: pipe ANTHROPIC_API_KEY to ci-deploy.sh via stdin

Rotating the Anthropic key is now a GH Secret update + re-run —
no SSH-and-edit on the VM.

## 2026-04-17 · c45ec13 · auto
Merge fix/ci-migration-ai-log-fk: unblock CI + deploy

Alembic upgrade no longer fails on a fresh DB (FK to the un-migrated
ai_customization_logs table dropped, UUID type aligned with repo
convention).

## 2026-04-17 · 89966cc · auto
Merge feat/manual-link-review-priority-applied

Three sales-workflow features shipped together:

* **Submit job link** — paste an ATS URL, server fetches + scores the
  posting through the same pipeline the scanners use.
* **Review queue prioritization** — today / yesterday / older date
  buckets + team-wide best resume fit ordering.
* **Applied action** — marks a job submitted with an immutable snapshot
  of the resume text and score used at submit time.

<!-- Earlier releases are tracked in docs/releases/ as per-round writeups -->
<!-- RELEASES_LOG_END -->
