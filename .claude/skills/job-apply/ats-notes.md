# ATS field notes

Detect the ATS from the URL host, then use these as a starting map.
Always confirm against `read_page` — boards customize forms. Labels
win over guesses.

## Greenhouse — `boards.greenhouse.io/<co>/jobs/<id>` or `job-boards.greenhouse.io`
- Click "Apply" / "Apply for this job" to reveal `#application_form`.
- `first_name`, `last_name`, `email`, `phone`
- Resume: `input[type=file]` near "Resume/CV" (also offers Dropbox/
  Google Drive; ignore those, use `file_upload`).
- Cover letter: file input OR "Enter manually" toggle → textarea. Prefer
  the textarea.
- Custom questions: labeled blocks, mix of text, select, multi-select
  (react-select — click, type, Enter), yes/no radios.
- LinkedIn/website: usually plain text inputs under "Links".
- EEO block at the bottom ("Voluntary Self-Identification"): every
  select has a "Decline To Self Identify" option.
- Submit: `#submit_app` / button "Submit Application".
- Some boards add hCaptcha on submit → hard stop.

## Lever — `jobs.lever.co/<co>/<id>` → `/apply`
- `name` (single full-name field), `email`, `phone`, `org` (current company)
- `urls[LinkedIn]`, `urls[GitHub]`, `urls[Portfolio]`, `urls[Other]`
- Resume: `input[name=resume]` file.
- `comments` = "Additional information" textarea — this is the cover
  letter slot.
- Custom: `cards[<id>][field<n>]` inputs, labeled.
- EEO: `eeo[gender]`, `eeo[race]`, `eeo[veteran]`, `eeo[disability]` —
  each has a decline option.
- Consent checkbox for data processing is often required.
- Submit: button "Submit application".

## Ashby — `jobs.ashbyhq.com/<co>/<id>` → `/application`
- React app; refs change between renders — re-`read_page` after any
  step change. Use `find` by label text.
- `_systemfield_name`, `_systemfield_email`, `_systemfield_phone`,
  `_systemfield_location` (autocomplete — type city, pick suggestion),
  `_systemfield_resume` (file).
- Custom questions are labeled; selects are custom dropdowns (click,
  then click option).
- Submit: button "Submit Application".

## Workable — `apply.workable.com/<co>/j/<id>` → `/apply`
- Multi-step. `firstname`, `lastname`, `email`, `phone`, `address`.
- Resume upload FIRST — Workable parses it and pre-fills; re-read the
  page afterwards and correct anything it got wrong.
- `summary` = cover letter/summary textarea.
- Custom questions per step; "Next" between steps, "Submit application"
  at the end.

## BambooHR — `<co>.bamboohr.com/careers/<id>`
- Standard labeled fields; resume file input; usually no EEO.

## Generic / company career page
- `read_page filter:interactive`, map by nearest label text.
- Multi-step wizards: fill, screenshot, "Next", repeat; treat the final
  step as the review gate.

## No-go (account creation required → skip, log `account_required`)
Workday (`myworkdayjobs.com`), iCIMS (`icims.com`), Taleo (`taleo.net`),
SuccessFactors (`successfactors.com`), Oracle HCM (`oraclecloud.com`),
LinkedIn Easy Apply. These need a per-company login; hand them to the
user with the URL.

## Always
- Hidden "honeypot" inputs (visually hidden, odd names) — never fill.
- CAPTCHA / Turnstile / "verify you're human" → hard stop, user solves.
- After submit, confirm a success state before logging `submitted`:
  a thank-you page, "Application submitted", or the form disappearing.
