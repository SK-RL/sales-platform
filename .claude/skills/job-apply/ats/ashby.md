<!-- Loaded on demand by SKILL.md: read ONLY the file matching the job's host. -->

## Ashby — `jobs.ashbyhq.com/<co>/<id>` → `/application`
- React app; refs change between renders — re-`read_page` after any
  step change. Use `find` by label text.
- `_systemfield_name`, `_systemfield_email`, `_systemfield_phone`,
  `_systemfield_location` (autocomplete — type city, pick suggestion),
  `_systemfield_resume` (file).
- Custom questions are labeled; selects are custom dropdowns (click,
  then click option).
- Submit: button "Submit Application".

## Filling — typed input only (verified live, Supabase)
**`form_input` does not work on Ashby.** It sets the DOM value and the
field looks correct on screen and in `read_page`, but Ashby's internal
state never sees it and the submit is rejected with
"Missing entry for required field: …" for fields that visibly contain
data. Greenhouse tolerates `form_input` (it keeps React's `_valueTracker`
in sync); Ashby does not.

So on Ashby: click the field, then `type`. Use `cmd+a` first when
replacing an existing value. This was caught on a real submit, not in
review — the first Supabase attempt failed on Email, Country of
Residence, Github Profile and Linkedin, all of which were displaying
their values at the time.

Common Supabase/Ashby field set (ids are stable across their postings):
`_systemfield_name`, `_systemfield_email`, `_systemfield_resume` (file),
then per-board custom ids for Passport Country, Country of Residence,
"Are you over the age of 18?" (a Yes/No button pair, not a select),
Github Profile, Linkedin, and free-text questions.

Two file inputs exist on the page: an "Autofill from resume" one at the
top and the real `_systemfield_resume`. Upload to the latter — `find`
returns both, so pick by label, and never trust the first match.

## Confirming a submit
Success is a green banner reading exactly "Your application was
successfully submitted", **and** the form disappearing, **and** zero
`Missing entry for required field` strings. Do not match on loose words
like "thank" — Supabase renders an "application limits" notice containing
"Thanks for your interest" on the *unsubmitted* page, which reads as
success to a naive check and did exactly that once.

Supabase caps applications at **3 per 60-day period** per candidate, so
slots there are worth spending deliberately.

## Verified 2026-09-10 (TensorWave x4, Supabase)

**Refs go stale on first render.** `read_page` refs captured right after
load are dead by the time the React form settles — a batch of
click-by-ref + type reported success on every action and left every
field empty. Take a screenshot, then click by **coordinate**. Verify one
field before filling the rest.

**Uploading the resume resets scroll to the top of the form.** Anything
you click at a remembered offset afterwards hits the wrong element.
Always re-scroll and screenshot after an upload, before the next click.

**Typed values can still submit as "missing".** Supabase rejected Email,
Passport Country and Country of Residence as missing while all three
visibly contained the right text. React's state never saw them. Fix:
triple-click, `cmd+a`, `Delete`, retype, then press **Tab** to blur.
The blur is what commits it.

**Location is a combobox, not a text field.** Type the city, wait ~3s,
then click the dropdown row. Typing alone leaves it unset.

**Dead postings are common.** `/application` on a closed role renders
"Job not found" with no error. Always `get_page_text` before filling —
several fills went into a 404 page before this check was added.

**Work Authorization is usually phrased inverted:** "Will you now or in
the future require *authorization* to work in the United States?" means
sponsorship. Answer **No**. See answers.md.

**TensorWave/Ashby consent block.** A required "Candidate Consent
Acknowledgment" (interview recording + Metaview/BrightHire AI
summarization) sits above Submit. This is *their* use of AI on the
interview, not a restriction on the applicant — not an AI-authorship
prohibition. Standing answer: agree.

**Conditional fields silently clear their parent (Camunda, 2026-09-10).**
Camunda's form asks "Are you legally eligible to work in the country
where you're planning to work from?" and then, *conditional on it*, "select
the status that allows you to work and live in that Country". Answering
the child re-rendered the parent and dropped its value — the Yes button
still looked selected, but submit reported it missing. Three attempts.

Two rules follow:
- Set a conditional parent **last**, after every dependent field.
- When a Yes/No looks selected but submit says it is missing, click the
  *other* option and then back. Re-clicking the already-highlighted one
  is a no-op and changes nothing.

Never batch a click on a conditional field with the next click: the
layout shifts underneath and the following coordinate lands somewhere
else. One click, one screenshot. On this form a batched pair set the
answer to **No** — the opposite of the truth — before it was caught.
