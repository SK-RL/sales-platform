# Own-words attestations — hard stop for generated text

Some employers require the applicant to attest that the application is
**their own words**, and say outright that generated content disqualifies
it. This is not a style preference. Ticking that box while submitting
text this skill composed is a false statement to the employer, and it is
also self-defeating: the stated penalty is disqualification.

Found live on Canonical (Greenhouse `question_42871448`), a **required**
dropdown, verbatim:

> During this application process I agree to use only my own words. I
> understand that plagiarism, the use of AI or other generated content
> will disqualify my application.

Canonical is not an edge case for us — it was 5 of the top 6 Greenhouse
global-remote relevant jobs in the platform on 2026-09-10.

## Detect
Before composing ANY free text, scan the full form text (labels,
descriptions, checkbox and dropdown text) case-insensitively for:

`my own words` · `your own words` · `own words` · `use of AI` ·
`AI-generated` · `AI generated` · `generated content` ·
`without the use of AI` · `not use AI` · `no AI` · `plagiarism` ·
`unaided` · `own work`

## Then
If any match is present, this job is **attestation-gated**:

1. **Do not compose free-text answers.** No cover letter, no "why us",
   no long-form answers written by this skill.
2. Short factual fields stay fine — name, email, phone, location, links,
   notice period, salary, yes/no eligibility. Those are the user's data
   being transcribed, not authored prose.
3. For every free-text box, ask the user to dictate the answer in their
   own words. Put their text in verbatim. Do not "polish", reword,
   expand, or fix grammar — verbatim means verbatim.
4. Only after their own words are in place may the attestation be
   answered, and the user answers it at the review gate, not the skill.
5. If the user does not want to write them, **skip the job**. Log
   `attestation_gated` with the matched phrase. Say plainly why.

Never tick, select, or otherwise affirm one of these attestations on the
user's behalf when any composed text is in the form. There is no version
of this task where that is acceptable, including when asked directly.

## Report it
The review block must show the matched clause verbatim and which answers
are the user's own words, so the person clicking submit knows exactly
what they are attesting to.
