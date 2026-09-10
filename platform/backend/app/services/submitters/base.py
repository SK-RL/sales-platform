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
_HUMAN_REQUIRED_MARKERS: tuple[str, ...] = (
    "recaptcha",
    "hcaptcha",
    "cf-turnstile",
    "verify you are human",
    "are you a robot",
    "create an account to apply",
    "sign in to apply",
    "verification code has been sent",
    "enter the code we emailed",
)


def detect_human_wall(page_html: str) -> str | None:
    """Return the marker that means we can't proceed unattended, if any."""
    haystack = (page_html or "").lower()
    for marker in _HUMAN_REQUIRED_MARKERS:
        if marker in haystack:
            return marker
    return None


def coerce_option(value: str, options: list[str]) -> str | None:
    """Map a free-text answer onto one of the ATS's allowed options.

    Returns ``None`` when no confident mapping exists — the caller must
    then treat the field as unplaceable rather than picking the closest
    option. A select whose options are ["Yes", "No"] and an answer of
    "Prefer not to say" has no correct choice, and guessing one is
    exactly the class of bug F346 exists to prevent.
    """
    if not options:
        return value or None
    v = (value or "").strip()
    if not v:
        return None
    lowered = {opt.lower().strip(): opt for opt in options if isinstance(opt, str)}
    if v.lower() in lowered:
        return lowered[v.lower()]
    # Common yes/no phrasings — an exact semantic match, not a guess.
    truthy = {"yes", "true", "y", "1"}
    falsy = {"no", "false", "n", "0"}
    if v.lower() in truthy and "yes" in lowered:
        return lowered["yes"]
    if v.lower() in falsy and "no" in lowered:
        return lowered["no"]
    return None
