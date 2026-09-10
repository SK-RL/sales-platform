<!-- Aggregator hop. Read alongside ats/_shared.md and the destination ATS file. -->

## Himalayas — `himalayas.app/companies/<co>/jobs/<slug>`

Himalayas is not an ATS; it is a listing that hands off to the
employer's real application. It is also the single biggest source in
this platform (~47% of fresh job flow), so this hop matters.

**Logged out it is a dead end.** Cloudflare interstitial ("Just a
moment…") on load, and "Apply now" resolves to
`himalayas.app/signup/talent?redirect=…` — an account-creation wall,
which is a hard stop. Verified live.

**Logged in it works.** Use the Chrome profile that has Himalayas
signed in. Then:

1. Load the job URL. The Cloudflare check clears itself in a few
   seconds for a real browser profile — wait, do not try to defeat it.
2. Click **Apply now**. A modal appears offering *"Generate cover letter
   with AI"* and *"Generate resume with AI"*. **Never use these.** They
   produce exactly the generic output `voice.md` exists to prevent, and
   they are disqualifying on any employer with a no-AI clause.
3. Click **"I'm ready to apply"**. This opens a new tab at the
   employer's real ATS with `?ref=himalayas.app` tracking, e.g.
   `careers.tether.io/o/<slug>/c/new`.
4. From there, identify the destination ATS and read its own file.

The platform stores only the Himalayas URL, so the employer's real
apply link is discovered at this step, not from our data.

RemoteDXB and WeWorkRemotely are also aggregators — expect a similar
outbound hop rather than an inline form.

**Duplicate warning:** the same posting appears under several sources
(a Censys job surfaced both as `greenhouse` and as
`remotedxb/systems-engineer-censys`). Platform dedupe is per `job_id`,
so check company + title against `log/applied.jsonl` before filling.
