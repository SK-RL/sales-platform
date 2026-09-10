"""Server-side application submitters — the contract every ATS adapter implements.

Why an adapter per ATS
----------------------
There is no generic "submit a job application" API. The public job-board
APIs we already read from (Greenhouse, Lever, Ashby) are **read-only for
third parties** — their documented application-POST endpoints authenticate
as the *employer* with that employer's API key, which we will never hold.
So server-side submission means driving the real application form in a
real browser, and every ATS renders a different DOM.

That is why the shape here is a registry of per-platform adapters rather
than one clever universal filler: a selector set that works on Greenhouse
is meaningless on Workday. It also means adding platform coverage is
bounded, reviewable work (one adapter, one test) instead of an ever-more-
conditional mega-function.

The gate lives above this layer
-------------------------------
Adapters do not decide *whether* to submit. ``apply_task`` runs the F346
gate (``blocking_gaps`` + ``extraction_mode``) and only calls an adapter
once it has an extracted schema and a confidently resolved answer for
every required field. An adapter that is handed a field it cannot place
must fail loudly (``unplaceable_fields``) rather than skip it — silently
dropping a required answer is how you submit a form that says something
you didn't say.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SubmitField:
    """One resolved answer, ready to place into the ATS form."""

    field_key: str
    label: str
    field_type: str  # text | textarea | select | multi_select | file | boolean
    value: str
    required: bool = False
    options: list[str] = field(default_factory=list)
    # F350 — non-empty when several fields are alternatives satisfying a
    # single ATS question (Greenhouse "Resume/CV" -> resume | resume_text).
    alternative_group: str = ""


@dataclass
class SubmitOutcome:
    """What happened when we drove the form.

    ``status`` is deliberately narrow:

    * ``submitted`` — the ATS acknowledged the application. Only set
      this when we have positive evidence (a confirmation string, a
      redirect to a known success URL). "No exception was raised" is
      not evidence; a form that silently failed validation also raises
      nothing, and recording that as submitted is how a user ends up
      believing they applied to a job they didn't.
    * ``failed`` — we tried and could not complete. Retryable.
    * ``blocked`` — we did not try, because the page asked for
      something we won't do unattended (a CAPTCHA, an account login,
      an emailed verification code). Not retryable without a human;
      the task routes these to ``needs_user``.
    """

    status: str  # submitted | failed | blocked
    confirmation_text: str | None = None
    screenshot_keys: list[str] = field(default_factory=list)
    detected_issues: list[str] = field(default_factory=list)
    unplaceable_fields: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "submitted"


class BlockedBySite(RuntimeError):
    """The page needs a human — CAPTCHA, login wall, or emailed OTP.

    Raised by adapters so the task can distinguish "this will never work
    unattended" from "this failed, try again in ten minutes". Retrying a
    CAPTCHA just burns quota and looks like abuse to the ATS.
    """


class BaseSubmitter(ABC):
    """One ATS. Fill its form, prove it went through."""

    #: Platform key, matching ``Job.platform`` and the fetcher map.
    platform: str = ""

    @abstractmethod
    async def submit(
        self,
        *,
        job_url: str,
        fields: list[SubmitField],
        resume_path: str | None = None,
        cover_letter_text: str | None = None,
        dry_run: bool = False,
    ) -> SubmitOutcome:
        """Drive the application form to completion.

        ``dry_run`` fills every field and stops immediately before the
        final submit click, returning ``status="submitted"`` with
        ``detected_issues=["dry_run"]``. This mirrors the existing
        dry-run contract in ``ApplicationSubmission`` and is the only
        safe way to exercise an adapter against a live posting.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Substrings that mean "a human is required". Checked against the page
# text before we attempt to fill. Cheap, and catches the common walls
# before we waste a browser session on them.
#
# NOTE the absence of a bare "recaptcha" here. That was the original
# marker and it made this function useless: a live Greenhouse board
# (job-boards.greenhouse.io) loads reCAPTCHA **Enterprise v3** on every
# posting —
#
#   <script src="…/recaptcha/enterprise.js?render=6Lfmcbcp…">
#   <textarea name="g-recaptcha-response" …>
#
# — which is score-based and invisible. There is no challenge to solve;
# the token is minted from behavioural signals. Treating its mere
# presence as a wall meant every single Greenhouse application would
# have returned `blocked` -> needs_user, i.e. the whole feature would
# have quietly done nothing while looking correctly cautious.
#
# What genuinely needs a human is the *interactive* variants, which are
# identifiable by their challenge iframe / explicit widget rather than
# by the word "recaptcha" appearing anywhere on the page.
_HUMAN_REQUIRED_MARKERS: tuple[str, ...] = (
    # reCAPTCHA v2 checkbox — the anchor iframe is the clickable widget.
    "recaptcha/api2/anchor",
    # reCAPTCHA v2 image challenge popup.
    "recaptcha/api2/bframe",
    'class="g-recaptcha"',
    "class='g-recaptcha'",
    # hCaptcha's interactive checkbox widget. F359 — the old marker was
    # the single script filename "hcaptcha.com/1/api.js", and Lever
    # loads "js.hcaptcha.com/1/secure-api.js" instead, so a live Lever
    # apply page carrying a real hCaptcha checkbox came back as NO wall.
    # That is the dangerous direction: we'd have driven a form we cannot
    # submit and looked like abuse to the ATS.
    #
    # These target the widget rather than any one script URL, so an
    # invisible/score-based hCaptcha still doesn't trip them — the same
    # discrimination the reCAPTCHA markers make.
    'class="h-captcha"',
    "class='h-captcha'",
    "checkbox for hcaptcha",
    "cf-turnstile",
    # DataDome challenge interstitial. F364 — SmartRecruiters' apply
    # flow serves headless Chromium a 3 KB page with an empty body, no
    # app root and these two fingerprints, where a real browser gets the
    # full form. Same vendor the scraper docstring already notes blocks
    # Wellfound. Without a marker an adapter would sit on a 10 s selector
    # timeout and report "browser error" instead of the real reason.
    # Getting past it is detection evasion, which we do not do; the
    # correct outcome is blocked -> review queue.
    "captcha-delivery.com",
    "var dd={'rt':'c'",
    # Plain-language walls.
    "verify you are human",
    "are you a robot",
    "create an account to apply",
    "sign in to apply",
    "verification code has been sent",
    "enter the code we emailed",
)


def detect_human_wall(page_html: str) -> str | None:
    """Return the marker that means we can't proceed unattended, if any.

    Deliberately does NOT fire on invisible/score-based bot protection —
    see ``_HUMAN_REQUIRED_MARKERS``. A false positive here is not a safe
    failure: it turns every application into ``needs_user`` and the
    feature silently accomplishes nothing.
    """
    haystack = (page_html or "").lower()
    for marker in _HUMAN_REQUIRED_MARKERS:
        if marker in haystack:
            return marker
    return None


def coerce_option(value: str, options: list[Any]) -> str | None:
    """Map a free-text answer onto one of the ATS's allowed options.

    Returns ``None`` when no confident mapping exists — the caller must
    then treat the field as unplaceable rather than picking the closest
    option. A select whose options are ["Yes", "No"] and an answer of
    "Prefer not to say" has no correct choice, and guessing one is
    exactly the class of bug F346 exists to prevent.

    Handles both option shapes our extractors produce, because the two
    ATSes genuinely differ and normalising them away would lose
    information:

    * Greenhouse — ``[{"value": "1", "label": "Yes"}, ...]``. The label
      is what the candidate reads, the value is what the ``<option>``
      carries, so we match on either and always return the *value*.
    * Recruitee — ``["SEO / SEM", "Social Media", ...]``. Value and
      label are the same string.

    This was a live defect: the dict shape fell through the old
    ``isinstance(opt, str)`` filter, so every Greenhouse select resolved
    to ``None``. It failed safe (required selects blocked rather than
    guessed) but no Greenhouse select would ever have auto-filled.
    """
    if not options:
        return value or None
    v = (value or "").strip()
    if not v:
        return None

    # submit_value -> the texts that should select it
    candidates: list[tuple[str, set[str]]] = []
    for opt in options:
        if isinstance(opt, dict):
            submit_value = str(opt.get("value", "")).strip()
            label = str(opt.get("label", "")).strip()
            texts = {t.lower() for t in (submit_value, label) if t}
            if submit_value or label:
                candidates.append((submit_value or label, texts))
        elif isinstance(opt, str) and opt.strip():
            candidates.append((opt.strip(), {opt.strip().lower()}))

    if not candidates:
        return None

    needle = v.lower()
    for submit_value, texts in candidates:
        if needle in texts:
            return submit_value

    # Common yes/no phrasings — an exact semantic match, not a guess.
    # "1"/"0" are deliberately NOT synonyms here: Greenhouse uses them
    # as literal option *values* (value="1" label="Yes"), so they are
    # already handled by the exact pass above. Treating them as words
    # would make "0" ambiguous between "the option whose value is 0"
    # and "no" — precisely the ambiguity this function refuses.
    truthy = {"yes", "true", "y"}
    falsy = {"no", "false", "n"}
    target: set[str] | None = None
    if needle in truthy:
        target = {"yes"}
    elif needle in falsy:
        target = {"no"}
    if target:
        for submit_value, texts in candidates:
            if texts & target:
                return submit_value
    return None
