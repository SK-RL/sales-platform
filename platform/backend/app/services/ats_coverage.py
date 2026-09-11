"""What auto-apply does on each ATS, for the end-user guide.

F393. One catalogue, served by ``GET /applications/ats-coverage`` and
rendered in the platform guide (Docs → "Auto-apply: what is automatic on
each site") and linked from the Auto-apply page. The *capability* of
each row (automatic / needs you to submit / link only / not supported)
is derived from the real registries — the submitter registry, the
question extractors and ``KNOWN_HUMAN_WALLS`` — so the page can never
claim more than the code does. Only the human-facing wording lives
here.

Levels:
  automatic  — we fill AND submit the real form; you only answer what
               the gate stops on.
  review     — we read the real form and fill your answers, but the
               vendor puts a human check (CAPTCHA) on submit, so you
               press the button in your own browser.
  link       — we find the posting and give you the link; the form
               itself is walled.
  closed     — we can't help on this site (account-only / bot wall);
               a pasted link is refused with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AtsEntry:
    platform: str
    name: str
    link_example: str
    automatic: list[str] = field(default_factory=list)   # what we do without you
    you: list[str] = field(default_factory=list)         # action items for the user
    notes: list[str] = field(default_factory=list)


_CATALOGUE: tuple[AtsEntry, ...] = (
    AtsEntry("greenhouse", "Greenhouse", "boards.greenhouse.io/{company}/jobs/{id}",
             ["Reads the real form (custom questions included)", "Fills your résumé, identity fields and Answer Book answers", "Submits and waits for Greenhouse's confirmation"],
             ["Answer any question the gate stops on (work authorization, sponsorship, salary, EEO are never guessed)"],
             ["Invisible reCAPTCHA v3 — no checkbox, nothing for you to do"]),
    AtsEntry("ashby", "Ashby", "jobs.ashbyhq.com/{company}/{id}",
             ["Reads the rendered form", "Fills text, Yes/No buttons, dropdowns, location combobox and résumé", "Submits and checks for \"successfully submitted\""],
             ["Answer gate stops", "Optional cover-letter file is skipped unless you attach one"],
             ["Invisible reCAPTCHA v3"]),
    AtsEntry("workable", "Workable", "apply.workable.com/{company}/j/{id}",
             ["Reads the form (QA_ questions with their real labels)", "Fills fields, radio groups and résumé", "Submits"],
             ["Answer gate stops"], []),
    AtsEntry("recruitee", "Recruitee", "{company}.recruitee.com/o/{job}",
             ["Reads the form", "Fills and submits"], ["Answer gate stops"], []),
    AtsEntry("breezy", "Breezy HR", "{company}.breezy.hr/p/{id}",
             ["Reads the form incl. section questions and EEO", "Never touches the hidden honeypot fields", "Submits and checks the success message"],
             ["Answer gate stops (EEO is always yours)"], []),
    AtsEntry("personio", "Personio", "{company}.jobs.personio.com/job/{id}",
             ["Loads the English form", "Fills fields, custom attributes and CV", "Submits"],
             ["Answer gate stops"], ["Forms in other languages are switched to English automatically"]),
    AtsEntry("rippling", "Rippling", "ats.rippling.com/{company}/jobs/{id}",
             ["Reads the form", "Fills fields, comboboxes, custom radio questions and résumé", "Submits"],
             ["Answer gate stops"], ["Invisible Turnstile — nothing to click"]),
    AtsEntry("teamtailor", "Teamtailor", "{company}.teamtailor.com/jobs/{id}",
             ["Opens the application form", "Fills fields, choice groups, consent boxes and CV", "Submits"],
             ["Answer gate stops", "Privacy consent is ticked only when your Answer Book says so"], []),
    AtsEntry("pinpoint", "Pinpoint", "{company}.pinpointhq.com/en/postings/{id}",
             ["Opens the form behind \"Apply\"", "Fills fields, Yes/No questions, dropdowns (incl. country) and CV", "Submits"],
             ["Answer gate stops", "Equality-monitoring dropdowns (gender, ethnicity, …) are always yours"],
             ["State/province is only asked for some countries"]),
    AtsEntry("jobvite", "Jobvite", "jobs.jobvite.com/{company}/job/{id}",
             ["Passes the data-consent step with the region you chose", "Fills fields, radio questions, dropdowns and résumé", "Submits"],
             ["Pick your \"Location of residence and language\" once — it's a question in the review", "Answer gate stops"],
             ["Invisible reCAPTCHA", "Cover letters are file-only on Jobvite, so text cover letters are not sent"]),
    AtsEntry("hireology", "Hireology", "careers.hireology.com/{company}/{id}",
             ["Reads the public form schema", "Fills fields, address, referral question and résumé", "Submits"],
             ["Answer gate stops"], ["Text-message consent is left unticked unless you say otherwise"]),
    AtsEntry("dover", "Dover", "app.dover.com/apply/{company}/{id}",
             ["Reads the posting's question list", "Fills name, email, LinkedIn, phone, custom questions and résumé", "Submits"],
             ["Answer gate stops", "LinkedIn URL is usually required — keep it in your Answer Book"],
             ["Invisible Turnstile"]),
    AtsEntry("gem", "Gem", "jobs.gem.com/{company}/{id}",
             ["Reads the form schema", "Fills fields, custom questions and résumé", "Submits with \"Apply without saving\" — no Gem profile is created"],
             ["Answer gate stops", "Voluntary self-identification questions are always yours"],
             ["Invisible hCaptcha"]),
    AtsEntry("lever", "Lever", "jobs.lever.co/{company}/{id}",
             ["Reads the real form and prepares every answer"],
             ["Open the posting, paste the prepared answers, tick the hCaptcha and submit", "Then press \"Mark applied\" on the review page"],
             ["Lever puts an hCaptcha checkbox on every application"]),
    AtsEntry("bamboohr", "BambooHR", "{company}.bamboohr.com/careers/{id}",
             ["Reads the form and custom questions from BambooHR's public API"],
             ["Submit in your own browser (reCAPTCHA checkbox), then \"Mark applied\""], []),
    AtsEntry("jazzhr", "JazzHR", "{company}.applytojob.com/apply/{code}",
             ["Reads the application page"],
             ["Submit in your own browser (reCAPTCHA checkbox), then \"Mark applied\""], []),
    AtsEntry("zoho", "Zoho Recruit", "{company}.zohorecruit.com/jobs/Careers/{id}/{title}",
             ["Finds the posting and keeps it in your jobs"],
             ["Apply on the site yourself — the \"I'm interested\" form ends with an image CAPTCHA"], []),
    AtsEntry("smartrecruiters", "SmartRecruiters", "jobs.smartrecruiters.com/{company}/{id}",
             ["Finds the posting"],
             ["Apply in your own browser — DataDome challenges automated browsers"], []),
    AtsEntry("himalayas", "Himalayas (job board)", "himalayas.app/companies/{company}/jobs/{job}",
             ["Finds the employer's own form by company + title and applies there when it's on a supported ATS"],
             ["If no employer form is found, apply on Himalayas yourself"],
             ["Himalayas' own pages are behind a Cloudflare challenge"]),
    AtsEntry("yc_waas", "Y Combinator — Work at a Startup", "workatastartup.com/jobs/{id}",
             [], ["Apply with your own YC account — applications only go through account.ycombinator.com"], []),
    AtsEntry("join", "JOIN", "join.com/companies/{company}/{id}",
             [], ["Apply yourself — JOIN verifies your email (code or Google sign-in) before showing the form"], []),
    AtsEntry("polymer", "Polymer", "jobs.polymer.co/{company}/{id}",
             [], ["Apply yourself — every posting page is behind a Cloudflare bot check"], []),
    AtsEntry("careerplug", "CareerPlug", "{company}.careerplug.com/jobs/{id}",
             [], ["Apply yourself — CareerPlug asks you to create an account first"], []),
    AtsEntry("workday", "Workday", "{company}.myworkdayjobs.com/…",
             [], ["Apply yourself — Workday needs an account on the employer's site"], []),
    AtsEntry("linkedin", "LinkedIn", "linkedin.com/jobs/view/{id}",
             [], ["Open the posting's own apply link and paste that instead"], []),
)

_LEVEL_LABEL = {
    "automatic": "Automatic",
    "review": "We fill, you submit",
    "link": "Link only",
    "closed": "Not supported",
}


def _level(platform: str) -> str:
    from app.fetchers import FETCHER_MAP
    from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, human_wall_for
    from app.services.submitters import auto_submittable_platforms

    if platform in auto_submittable_platforms():
        return "automatic"
    if platform in SUPPORTED_QUESTION_PLATFORMS:
        return "review"
    if platform in FETCHER_MAP or human_wall_for(platform):
        return "link"
    return "closed"


def ats_coverage() -> list[dict]:
    """Rows for the guide, in display order: automatic first."""
    from app.fetchers.questions import human_wall_for

    order = {"automatic": 0, "review": 1, "link": 2, "closed": 3}
    rows = []
    for e in _CATALOGUE:
        level = _level(e.platform)
        wall = human_wall_for(e.platform)
        rows.append({
            "platform": e.platform,
            "name": e.name,
            "level": level,
            "level_label": _LEVEL_LABEL[level],
            "link_example": e.link_example,
            "automatic": list(e.automatic),
            "you": list(e.you),
            "notes": list(e.notes) + ([f"{wall['vendor']}: {wall['reason']}"] if wall and level != "automatic" else []),
        })
    rows.sort(key=lambda r: (order[r["level"]], r["name"].lower()))
    return rows


def catalogued_platforms() -> set[str]:
    return {e.platform for e in _CATALOGUE}
