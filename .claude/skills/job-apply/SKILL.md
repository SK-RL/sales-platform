---
name: job-apply
description: Fill and submit job applications from the sales platform's relevant global-remote jobs using the user's own Chrome, then record every filled answer against that job's Application in the platform so it can be recalled. Use when the user says "apply to this job", "/job-apply <url>", "apply to the next N", "fill this application", or "prepare applications". Greenhouse first. Fills from profile.yaml, drafts free text in the user's voice (voice.md), uploads the resume, and always stops for review before submit. Refuses to compose text for employers that require the applicant's own words.
---

# job-apply

Operator procedure. All personal data comes from `profile.yaml` in this
directory and nowhere else. Read `voice.md` before writing any free text
and `ats-notes.md` for field maps.

## Browser
Use **Claude in Chrome** (`mcp__claude-in-chrome__*`), the user's real
Chrome. Two hard reasons: the in-app browser has no file-upload tool and
every ATS needs the resume PDF; and a real profile with history is what a
normal applicant looks like. Load once per session:
`ToolSearch "select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__find,mcp__claude-in-chrome__form_input,mcp__claude-in-chrome__file_upload,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__tabs_close_mcp,mcp__claude-in-chrome__javascript_tool"`.

## Setup (once)
1. No `profile.yaml`? Copy `profile.example.yaml` to `profile.yaml`, ask the
   user to fill it, stop. Never fill it yourself, never guess a value.
2. Check `resume.path` exists.
3. The platform must have an **active resume** selected (the record call
   needs it). If the record call returns 400 "No active resume", tell the
   user to pick one at /resume-score and retry.

## Commands
- `/job-apply <url> [url ...]` — specific postings
- `/job-apply next N` — pull N targets from the platform (Greenhouse only until told otherwise)
- `/job-apply status` — summary from the platform + local log

## Targets (`next N`)
Same-origin fetch from a platform tab, so cookie auth just works and no
token is ever handled:
1. Open `https://salesplatform.reventlabs.com/jobs`. If it shows the
   login page, ask the user to sign in in that tab. Never type credentials.
2. `javascript_tool`:
   `await fetch('/api/v1/jobs?role_cluster=relevant&geography=global_remote&status=new&platform=greenhouse&page_size=25&sort_by=relevance_score&sort_dir=desc').then(r=>r.json())`
   (if 422, drop the sort params).
3. From `items[]` keep `id`, `url`, `company_name`, `title`, `platform`.
4. Skip and report: already in `log/applied.jsonl`; same `company_name`
   applied within 30 days; host in the no-go list in `ats-notes.md`.
5. Take the first N remaining.

## Per job
1. New tab → job `url`. On legacy boards click "Apply" / "Apply for this
   job" to reveal the form; on modern Greenhouse the form is already there
   (see step 2).
2. Identify the ATS from the host (`ats-notes.md`). On modern Greenhouse
   (`job-boards.greenhouse.io`) the form is already inline — no Apply
   click — and fields are keyed by `id`, not `name`. If the URL bounces to
   the board root or `?error=true`, the posting is gone: skip, log
   `posting_gone`. Where a board offers **Autofill from resume**, upload
   the resume first, then re-`read_page` and check every prefilled field
   against `profile.yaml` — the profile always wins.
3. **Scan for an own-words / no-AI attestation before composing anything**
   (`attestations.md`). If the form has one, this job is attestation-gated:
   fill only factual fields, and every free-text box must be the user's own
   words, dictated by them and entered verbatim — or skip the job. Never
   affirm such an attestation on their behalf. Seen live on Canonical.
4. `read_page filter:interactive`. Map each labeled control to a profile
   key. Fill with `form_input`. Selects: pick the option matching the
   profile; nearest truthful option if no exact match; ask if none.
5. Free-text boxes (cover letter, why us, tell us about …): write per
   `voice.md`, grounded only in `profile.yaml`.
6. Resume: `file_upload` on the file input with `resume.path` (skip if
   autofill already attached it — verify the filename is shown).
7. EEO / voluntary self-identification: per `defaults.eeo` ("decline"
   picks the decline option everywhere). Never guess demographics.
8. Do not submit. Screenshot. Print the review block:
   company · title · url · each field as `label → value` (truncate long
   values) · full text of every free-text answer · anything left blank.

## Unknown required questions
No answer in `profile.yaml`: stop, ask the user the question verbatim,
then save their answer under `custom_answers` in `profile.yaml` (key =
question lowercased, punctuation stripped) so it is decided once.
Optional unknowns: leave blank and list them in the review block.

## Record in the platform (recall)
Every filled answer is stored against this job's Application so it can
be recalled from Job Detail → Application. Two calls, both same-origin
`fetch` from the platform tab (cookie auth, no tokens):

**1. Right after filling, before the review gate — `POST /api/v1/applications/record`**
```
await fetch('/api/v1/applications/record', {method:'POST',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify({
    job_id: '<id from GET /jobs>',        // OR job_url: '<pasted url>' (exactly one)
    ats_platform: 'greenhouse',
    notes: '<anything skipped / left blank>',
    answers: [ {label:'<field label as shown>', answer:'<value typed>', field_type:'text|textarea|select|multi_select|file|boolean', required:true} ]
  })}).then(r=>r.json())
```
Keep `application_id`. The row is created as `prepared` with
`apply_method: claude_routine`. Re-calling after an edit overwrites the
answers (only while still `prepared`). 404 = the URL isn't a job the
platform knows → record locally only and say so. 400 "No active resume"
→ ask the user to pick one at /resume-score.

**2. After a confirmed submit — `POST /api/v1/applications/<application_id>/confirm-submitted`**
```
{ submitted_at: new Date().toISOString(), job_url: '<url>', ats_platform: 'greenhouse',
  payload_json: { ats:'greenhouse', tab_url:'<url>' },      // metadata only — never IDs/DOB/passport
  answers: [ {question:'<label>', answer:'<value>', source:'learned'|'generated'} ],   // learned = from profile.yaml, generated = free text you wrote
  cover_letter_text: '<the free text, if any>',
  confirmation_text: '<the thank-you text seen on screen>',
  detected_issues: [] }
```
This flips the Application to `applied`, stores the submission with the
answers, adds the review + pipeline entries and the scoring feedback the
platform expects. Do not set status by hand; this call owns it.

**3. New answers the user gave during the run** — also push each one to
the Answer Book so it is decided once on the platform too:
`POST /api/v1/applications/<application_id>/promote-answer` with
`{question, answer}`.

## Review gate → submit
Wait for the user. Accept `submit`, `submit 1,3`, `submit all`, `skip N`,
`edit <field>: <text>`. On submit: click the submit control, wait, confirm
a success state (thank-you page / "Application submitted" / form gone),
screenshot. On a validation error: fix the flagged field once and
re-review. Never loop on submit.
Then: record `submitted` in the platform (above) and append to
`log/applied.jsonl`:
`{"ts","url","company","title","platform","job_id","application_id","status":"submitted|skipped|failed","reason"}`.

## Batch (`next N`)
Fill all N first, one tab each. Then one review pass listing all. Submit
the approved ones with a 20–40 s gap between clicks. Cap 10 per run.

## Hard stops
- Visible CAPTCHA / Turnstile / "verify you're human": stop, name the tab,
  let the user solve it, continue on their word. Never attempt it.
- **Invisible reCAPTCHA v3 is active on Greenhouse** — no challenge appears,
  it scores the session, and a bad score can bin the application with no
  error. So: type into fields rather than injecting values via JS, work at
  human pace, and never try to influence or suppress the check.
- An own-words / no-AI attestation on the form (see `attestations.md`):
  no composed free text goes in that form, ever.
- Login or account-creation wall: skip, log `account_required`, give the URL.
- Fields asking for ID numbers, bank details, passwords: leave blank, flag.
- Never invent experience, dates, employers, numbers, or degrees. If the
  profile lacks it, the answer lacks it.
