# Where answers come from

Two stores, with one rule: **the Answer Book on the platform is the
source of truth for question→answer.** `profile.yaml` holds only what
the Answer Book can't (a local file path) or what is faster read locally
(identity/contact). Anything you learn during a run goes back to the
Answer Book so it is answered once, visible in the UI, reusable on any
machine, and survives this skill being deleted.

All calls are same-origin `fetch` from a logged-in platform tab.

## Pre-flight — before opening ANY job tab
```
await fetch('/api/v1/answer-book/required-coverage').then(r=>r.json())
```
Returns `{complete, total_required, total_filled, missing[], entries[]}`.
The 16 `missing` entries are salary minima (4, by geography), notice
period (2 formats), earliest start, work auth, sponsorship, relocation,
work mode, current location, and 4 EEO questions. They are seeded empty
on purpose — the platform requires the user to fill them so the answers,
EEO especially, are provably user-chosen and never a system default.

If `complete` is false: **stop before opening any tabs.** Show the
`missing` questions and ask the user to fill them (the answer-book setup
screen, or dictate them here and write them back). Opening ten tabs and
discovering on tab seven that we can't finish wastes the user's time and
leaves half-filled forms on real employer sites.

## Look up an answer
```
await fetch('/api/v1/answer-book?q=' + encodeURIComponent(text) + '&page_size=200').then(r=>r.json())
```
Substring match over question + answer. Match on the normalised question
(lowercase, punctuation stripped, spaces → `_`) before falling back to
fuzzy label matching. Entries with an empty `answer` count as unknown.

## Write an answer back
New answer learned during a run:
```
await fetch('/api/v1/answer-book', {method:'POST',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify({category:'custom', question:'<verbatim question>', answer:'<the user's words>'})})
```
`category` ∈ `personal_info | work_auth | experience | skills | preferences | custom`.
Filling one of the 16 required entries is a **PATCH** on its `id` from
`required-coverage` (they are `is_locked`, so don't POST a duplicate):
```
await fetch('/api/v1/answer-book/' + id, {method:'PATCH',
  headers:{'Content-Type':'application/json'}, body: JSON.stringify({answer:'<value>'})})
```
After a submitted application you may also promote an answer from the
submission: `POST /api/v1/applications/<app_id>/promote-answer`
with `{question, answer}`.

## Batch the questions — never interrupt per field
For a run of N jobs:
1. Open and read all N forms first. Do not fill yet.
2. For every field, resolve in order: Answer Book → `profile.yaml` →
   unknown.
3. Sort the unknowns into three buckets:
   - **factual-unknown** — salary, visa, clearance, dates, employer
     names. Only the user knows. Must ask.
   - **composable** — open prose that can be drafted from known facts
     (`voice.md`). Draft it; the user still sees it at the review gate.
   - **attestation** — see `attestations.md`. Never auto-answer.
4. Ask the whole factual-unknown set **once**, as a numbered list, saying
   which job each belongs to and whether it is required.
5. Write every answer back to the Answer Book, then fill all N forms.

Only *required* unknowns block a job. Leave optional unknowns blank and
list them in the review block.

## When no truthful answer exists
A required question you cannot answer truthfully in the user's favour
("do you hold an active TS/SCI clearance?" when they don't) is answered
**truthfully anyway**, or the job is skipped — the user's call at the
review gate. Never improve an answer by making it untrue. A false
answer on an application is fraud risk for the user, not a clever
optimisation.

## Quality check before the review gate
Every composed free-text answer must pass all four, or be rewritten:
1. Contains at least one concrete particular — a number, tool, system,
   or project name — traceable to the Answer Book or `profile.yaml`.
2. No banned phrasing and no em-dashes (`voice.md`).
3. Within the length guide for that field type.
4. Could not be pasted into a different company's form unchanged.

If an answer cannot pass (1) because the profile has no relevant fact,
do not invent one. Say so and ask the user for the fact.
