<!-- Loaded on demand by SKILL.md: read ONLY the file matching the job's host. -->

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
