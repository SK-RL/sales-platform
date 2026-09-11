"""Registry of server-side ATS submitters.

Adding a platform is: write the adapter, register it here, and add it to
``fetchers/questions.SUPPORTED_QUESTION_PLATFORMS`` if we can also extract
its form. Those two sets are deliberately separate — extraction and
submission are different capabilities and we gain them at different times:

* extract-only  → we can show the user the real form and let them fill it
  in their own browser, but can't submit for them.
* submit-only   → meaningless. Without an extracted schema we don't know
  what the form asks, so ``apply_task`` refuses to auto-submit anyway
  (F346). ``AUTO_SUBMITTABLE_PLATFORMS`` below is the intersection, and
  that is the set the apply gate actually consults.
"""

from __future__ import annotations

from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS
from app.services.submitters.base import (
    BaseSubmitter,
    BlockedBySite,
    SubmitField,
    SubmitOutcome,
)
from app.services.submitters.ashby import AshbySubmitter
from app.services.submitters.breezy import BreezySubmitter
from app.services.submitters.greenhouse import GreenhouseSubmitter
from app.services.submitters.personio import PersonioSubmitter
from app.services.submitters.pinpoint import PinpointSubmitter
from app.services.submitters.recruitee import RecruiteeSubmitter
from app.services.submitters.rippling import RipplingSubmitter
from app.services.submitters.teamtailor import TeamtailorSubmitter
from app.services.submitters.workable import WorkableSubmitter

_REGISTRY: dict[str, type[BaseSubmitter]] = {
    AshbySubmitter.platform: AshbySubmitter,
    BreezySubmitter.platform: BreezySubmitter,
    GreenhouseSubmitter.platform: GreenhouseSubmitter,
    PersonioSubmitter.platform: PersonioSubmitter,
    PinpointSubmitter.platform: PinpointSubmitter,
    RecruiteeSubmitter.platform: RecruiteeSubmitter,
    RipplingSubmitter.platform: RipplingSubmitter,
    TeamtailorSubmitter.platform: TeamtailorSubmitter,
    WorkableSubmitter.platform: WorkableSubmitter,
}


def get_submitter(platform: str) -> BaseSubmitter | None:
    """Instantiate the adapter for a platform, or None if we have none."""
    cls = _REGISTRY.get((platform or "").strip().lower())
    return cls() if cls else None


def register_submitter(cls: type[BaseSubmitter]) -> None:
    """Register an adapter. Used by tests to install a fake."""
    if not cls.platform:
        raise ValueError("submitter must declare a platform")
    _REGISTRY[cls.platform] = cls


def unregister_submitter(platform: str) -> None:
    """Remove an adapter. Test teardown only."""
    _REGISTRY.pop(platform, None)


def submit_platforms() -> frozenset[str]:
    """Platforms we can drive a form on."""
    return frozenset(_REGISTRY)


def auto_submittable_platforms() -> frozenset[str]:
    """Platforms we can both understand and submit — the apply gate's set."""
    return submit_platforms() & SUPPORTED_QUESTION_PLATFORMS


__all__ = [
    "BaseSubmitter",
    "BlockedBySite",
    "SubmitField",
    "SubmitOutcome",
    "auto_submittable_platforms",
    "get_submitter",
    "register_submitter",
    "submit_platforms",
    "unregister_submitter",
]
