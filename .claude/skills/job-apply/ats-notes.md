# ATS field notes

Detect the ATS from the URL host, then use these as a starting map.
Always confirm against `read_page` — boards customize forms. Labels
win over guesses.

## Greenhouse — `job-boards.greenhouse.io/<co>/jobs/<id>` (modern) or `boards.greenhouse.io/...` (legacy)
Verified live against Canonical 4717512 on 2026-09-10 (42 inputs).

- **Modern boards render the form INLINE** — the job URL already is the
  application page (`<title>` = "Job Application for …"). No "Apply"
  click. Legacy `boards.greenhouse.io` still needs the Apply button.
- **Fields are keyed by `id`, and `name` is empty.** Select on `id`:
  `#first_name`, `#last_name`, `#email`, `#phone` (type=tel, with its own
  country-code listbox), `#country`, `#resume` (type=file, label "Attach").
- Education repeater: `#school--0`, `#degree--0`, `#discipline--0`, plus an
  "Add another" button (`--1`, `--2` … for further rows).
- **Custom questions are `#question_<numeric id>`** and come in two shapes
  that look identical in a flat input dump:
    * plain text / textarea → fill directly;
    * single-select → the `#question_*` input carries the LABEL, and the
      actual control is a separate id-less combobox rendering "Select…".
      Click that combobox and pick from `[role=option]`; `form_input` on
      the `#question_*` input does NOT set a dropdown.
  Read the rendered page (or screenshot) to tell them apart — the input
  dump alone will mislead you.
- `[role=option]` is shared with the phone country-code list, so scope
  option queries to the open dropdown, never `document`-wide.
- Cover letter: an "Enter manually" button reveals a textarea. Prefer it
  over the file input.
- **Filling mechanics, verified live:** `form_input` on a ref sets the value
  AND keeps React's `_valueTracker` in sync, so the value really submits;
  keyboard `type` after a click works too. Coordinate clicks drift on this
  long form (the page shifts between reading a rect and clicking) — prefer
  a fresh `find`/`read_page` ref over coordinates.
- **After a resume upload the file input is REMOVED from the DOM**
  (`input[type=file]` count drops to 0) and the filename renders as a chip
  with an "✕". Verify the upload by looking for the filename in the page
  text, not by re-querying `#resume`.
- EEO block: `#gender`, `#hispanic_ethnicity`, `#veteran_status`,
  `#disability_status` — each has a decline option.
- Submit: button "Submit application" (the page may expose two matching
  buttons; use the one inside the form).
- **reCAPTCHA v3 is active** (`.grecaptcha-badge`, `window.grecaptcha`).
  It is invisible and scores behaviour rather than showing a challenge —
  see "Bot detection" below.

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

## Bot detection
Greenhouse runs **invisible reCAPTCHA v3** on the application form. There
is no puzzle to solve — it scores the session and a low score can bin the
application silently, with no error shown. Practical consequences:
- Type into fields; never set values by injecting JS into the page.
- Do not machine-gun a form. Fill at human pace, one field at a time.
- Never try to influence, suppress, or solve the reCAPTCHA. If a visible
  challenge ever appears, that is a hard stop for the user.

## Always
- Hidden "honeypot" inputs (visually hidden, odd names) — never fill.
- A visible CAPTCHA / Turnstile / "verify you're human" → hard stop.
- **Dead posting:** a job URL that redirects to the board root or carries
  `?error=true` means the posting is gone. Skip, log `posting_gone`.
  (Seen live: an Alpaca job still `status=new` in our DB had been pulled.)
- After submit, confirm a success state before logging `submitted`:
  a thank-you page, "Application submitted", or the form disappearing.
