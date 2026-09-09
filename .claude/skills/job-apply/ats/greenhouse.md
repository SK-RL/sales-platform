<!-- Loaded on demand by SKILL.md: read ONLY the file matching the job's host. -->

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
