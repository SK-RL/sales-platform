# Screening, and writing the verdict back

Two rules, learned the hard way in one session: **screen every posting
against the user's actual constraints before filling**, and **record the
verdict in the platform**. Skipping the second means re-screening the same
rejects next batch — 11 jobs were screened and thrown away in-conversation
before this was noticed.

## The platform's geography is not trustworthy
Measured on a live run: of 11 high-relevance recent candidates, **9 were
geographically impossible** for an India-based remote-only candidate, and
every one carried `geography_bucket: (none)` or `remote_policy:
worldwide`. Of 849 jobs marked `worldwide`, only 7 were direct-ATS fits
and the top one required US work authorisation.

So the score tells you the role matches the *skills*. It tells you nothing
about whether the person can hold it. Read the posting.

## Where the truth actually lives
| Check | Where to look | Real example |
|---|---|---|
| Base location | the header line under the job title | "Sofia, Bulgaria" · "Tel Aviv-Yafo" · "San Francisco Bay Area" |
| Region scope | title suffix or location field | "Remote, **Australia**" · "(AMER/APAC)" |
| Office requirement | body text and LI tags | "This is a Hybrid role" · `#LI-HYBRID` |
| Work authorisation | legal paragraph near the bottom | "contingent upon verification of ... authorization to work in the United States" |
| Genuinely global | explicit statement | "We hire globally ... there are no offices" (Supabase) |

The work-authorisation line is the nastiest: TensorWave's location field
says "Remote", the platform classified it `worldwide`, and the requirement
sits in boilerplate legal text. Grep the whole page for
`authorization to work in|authorised to work in|must be located|based in|
eligible to work` before trusting any "remote" label.

## Reject in the platform, with tags
```
POST /api/v1/reviews
{ job_id, decision: "reject", tags: [...], comment: "<verbatim evidence>" }
```
`decision` is `accept | reject | skip`. This flips the job to
`status: rejected`, so it leaves every `status=new` picker and is not
re-screened. Existing reviews in this platform all had empty tags — the
field was unused, and it is what makes rejections analysable.

**Tag vocabulary** (keep it stable, it is the whole point):

- `location-ineligible` — role is tied to a place the user cannot be
- `us-work-auth-required` — needs in-country authorisation they lack
- `hybrid-onsite` — requires office presence
- `geo-misclassified` — **platform said remote/global, posting disagrees**
- `role-mismatch` — wrong discipline (QA, sales, GTM)
- `level-mismatch` — intern/junior/manager against an IC senior
- `internship`
- `staffing-agency` — micro1, Toptal, Proxify: not the end employer
- `posting-dead` — 404 / `?error=true` / redirects to board root
- `attestation-gated` — employer forbids AI-assisted answers
- `not-an-employer` — VC fellowship, job board, aggregator artefact

The `comment` carries the **verbatim sentence** that decided it, not a
paraphrase. That is what makes the rejection auditable later and what
lets someone fix the classifier.

`geo-misclassified` is the highest-value tag here: it is direct feedback
that the platform's geography classification was wrong on that row, and
counting it tells you how big the problem is.

## Accepts are written for you
`confirm-submitted` already creates an accepted review ("Applied via
Claude routine") plus the pipeline entry and scoring feedback. Do not
post an accept review by hand.
