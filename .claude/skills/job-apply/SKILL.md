---
name: job-apply
description: Fill and submit job applications from the sales platform's relevant global-remote jobs using the user's own Chrome, recording every answer against that job's Application so it can be recalled. Use when the user says "apply to this job", "/job-apply <url>", "apply to the next N", "fill this application", or "prepare applications". Answers come from the platform Answer Book plus profile.yaml; free text is drafted in the user's voice. Always stops for review before submitting, and refuses to compose text for employers that require the applicant's own words.
---

# job-apply

Operator procedure. Answers come from the platform **Answer Book**
(`answers.md`) and `profile.yaml`. Read `voice.md` before writing any
free text, `attestations.md` before composing anything at all, and the
one `ats/<platform>.md` matching the job's host plus `ats/_shared.md`.

## Browser
Use **Claude in Chrome** (`mcp__claude-in-chrome__*`), the user's real
Chrome. Two hard reasons: the in-app browser has no file-upload tool and
every ATS needs the resume PDF; and a real profile with history is what a
normal applicant looks like. Load once per session:
`ToolSearch "select:mcp__claude-in-chrome__browser_batch,mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__find,mcp__claude-in-chrome__form_input,mcp__claude-in-chrome__file_upload,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__tabs_close_mcp,mcp__claude-in-chrome__javascript_tool"`

## Commands
- `/job-apply <url> [url ...]` — specific postings
- `/job-apply next N` — pull N targets from the platform
- `/job-apply status` — platform + local log summary

## Pre-flight — do this BEFORE opening any job tab
1. `profile.yaml` exists (else copy `profile.example.yaml`, ask the user
   to fill it, stop) and `resume.path` is a real file.
2. Open `https://salesplatform.reventlabs.com/jobs`. If it shows the
   login page, ask the user to sign in in that tab. **Never type
   credentials.**
3. Active resume selected — the record call needs one. If a later call
   returns 400 "No active resume", send the user to /resume-score.
4. `GET /api/v1/answer-book/required-coverage`. If `complete` is false,
   show the `missing` questions, ask the user to fill them, and **stop**.
   See `answers.md` for why this gate exists and how to write them back.

## Targets (`next N`)
Same-origin `fetch` from the platform tab — cookie auth, no tokens:
```
await fetch('/api/v1/jobs?role_cluster=relevant&geography=global_remote&status=new&platform=greenhouse&page_size=25&sort_by=relevance_score&sort_dir=desc').then(r=>r.json())
```
(drop the sort params if it 422s). Keep `id`, `url`, `company_name`,
`title`, `platform`. Skip and report: already in `log/applied.jsonl`;
same `company_name` applied within 30 days; host in the no-go list in
`ats/_shared.md`. Take the first N remaining. Cap 10 per run.

## Read all forms first, then ask once, then fill
Do not fill a form the moment you open it. For the whole batch:

1. **Open + read.** New tab per job. On modern Greenhouse the form is
   already inline; on legacy boards click "Apply for this job". If the
   URL bounces to the board root or `?error=true` the posting is gone —
   skip, log `posting_gone`.
   **Aggregator URLs are a hop, not a form.** himalayas / remotedxb /
   weworkremotely hand off to the employer's real ATS — follow it
   (`ats/himalayas.md`), then use the destination's file. Logged out,
   Himalayas is an account wall and a hard stop; use the Chrome profile
   that has it signed in.
2. **Identify the ATS** from the host and read `ats/<platform>.md` +
   `ats/_shared.md`.
3. **Scan the WHOLE form region for an own-words / no-AI clause**
   (`attestations.md`) before composing anything — full rendered text,
   not just `<label>` elements. Canonical puts it in a required dropdown
   label; Tether puts it in a plain callout div, which a label-only scan
   misses entirely. Then judge context: an instruction not to use AI
   gates the job, a job description praising AI fluency does not. If
   gated: factual fields only; free text must be the user's own words
   entered verbatim, or skip. Never affirm such an attestation yourself.
4. **Resolve every field**: Answer Book → `profile.yaml` → unknown.
   Sort unknowns into factual-unknown / composable / attestation
   (`answers.md`).
5. **Ask the factual unknowns once**, as one numbered list across the
   whole batch, noting the job and whether each is required. Write the
   user's answers back to the Answer Book.
6. **Fill.** `form_input` on a fresh ref keeps React's value tracker in
   sync, so values really submit; coordinate clicks drift on long forms.
   Type at human pace — Greenhouse scores the session with invisible
   reCAPTCHA v3 and machine-gunning a form can bin it silently. Never
   inject values via raw JS.
7. **Free text** per `voice.md`, then run the four-point quality check in
   `answers.md`. Rewrite anything that fails.
   **Check what the control actually is first.** Tether's "Why are you
   interested?" is a *video* question (Record / Upload Video) with a
   hidden companion text input — filling the text does not answer it. A
   question needing video, audio or a live task is the user's to do: say
   so plainly rather than filling around it.
8. **Resume**: `file_upload` on the file input with `resume.path`. After
   upload the file input is removed from the DOM and the filename shows
   as a chip — verify by page text.
9. **EEO / self-identification**: use the user's four EEO answers from
   the Answer Book. Never guess demographics, never default them.

## Record in the platform — BEFORE the review gate
So the answers survive even if the submit is skipped or fails:
```
await fetch('/api/v1/applications/record', {method:'POST',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify({
    job_id: '<id from GET /jobs>',        // OR job_url: '<pasted url>' (exactly one)
    ats_platform: 'greenhouse',
    notes: '<anything skipped or left blank>',
    answers: [ {label:'<field label as shown>', answer:'<value typed>', field_type:'text|textarea|select|multi_select|file|boolean', required:true} ]
  })}).then(r=>r.json())
```
Keep `application_id`. Creates a `prepared` row tagged `claude_routine`;
re-calling overwrites while still `prepared`, and 409s once submitted.
404 = the URL isn't a job the platform knows → log locally and say so.

## Review gate
Never submit unprompted. Print per job: company · title · url · each
field as `label → value` · the full text of every free-text answer ·
anything left blank · **any attestation clause verbatim**, so the person
clicking submit knows exactly what they are attesting to.

Accept `submit`, `submit 1,3`, `submit all`, `skip N`,
`edit <field>: <text>`. After an edit, re-record and re-review.

## Submit
Click submit, wait, and confirm a success state — thank-you page,
"Application submitted", or the form disappearing. On a validation error
fix the flagged field once and re-review; never loop on submit. Leave
20–40 s between submissions.

Then confirm to the platform:
```
await fetch('/api/v1/applications/<application_id>/confirm-submitted', {method:'POST',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify({
    submitted_at: new Date().toISOString(), job_url: '<url>', ats_platform: 'greenhouse',
    payload_json: { ats:'greenhouse', tab_url:'<url>' },   // metadata only — never IDs/DOB/passport
    answers: [ {question:'<label>', answer:'<value>', source:'learned'|'generated'} ],
    cover_letter_text: '<free text, if any>',
    confirmation_text: '<the thank-you text on screen>',
    detected_issues: [] })}).then(r=>r.json())
```
This owns the `applied` transition and its side effects. Do not set
status by hand.

**If the submit succeeded but this call fails**, the user has applied and
the platform doesn't know — the 30-day dedupe will offer the job again.
Retry once. If it still fails, say so loudly, write the local log entry
with `"status":"submitted","platform_recorded":false`, and give the user
the `application_id` and job URL so it can be reconciled. Never let this
fail silently.

Finally append to `log/applied.jsonl`:
`{"ts","url","company","title","platform","job_id","application_id","status":"submitted|skipped|failed","platform_recorded":true|false,"reason"}`

## Hard stops
- Visible CAPTCHA / Turnstile / "verify you're human": stop, name the
  tab, let the user solve it, continue on their word. Never attempt it.
- Invisible reCAPTCHA v3 on Greenhouse: never try to influence or
  suppress it. Type, pace, don't inject.
- Own-words / no-AI attestation: no composed text in that form, ever.
- Login or account-creation wall: skip, log `account_required`, hand over
  the URL.
- Fields asking for ID numbers, passport, bank details, passwords: leave
  blank and flag. Never send these to `payload_json` either.
- Never invent experience, dates, employers, numbers, or degrees, and
  never answer a question untruthfully to improve the odds. If the
  profile and Answer Book don't have it, ask — or skip.
