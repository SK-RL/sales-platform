"""Greenhouse application submitter.

Greenhouse renders its application form either on ``job-boards.greenhouse.io``
(the hosted board) or embedded in a company page via ``#grnhse_app`` iframe.
Both end up with the same field naming: the ATS's own question ids become
input ``name`` attributes (``job_application[answers_attributes][0][text_value]``
and friends), while the fixed identity fields keep stable ids —
``#first_name``, ``#last_name``, ``#email``, ``#phone``, ``#resume``.

We locate fields by the ``field_key`` the extractor gave us, because
``fetchers/questions._fetch_greenhouse_questions`` reads the same
``questions[].fields[].name`` values off the Job Board API. That symmetry
is the point: the schema we showed the user and the DOM we fill are keyed
identically, so a field can never be shown as answered and then filled
somewhere else.
"""

from __future__ import annotations

import logging

from app.services.playwright_browser import BrowserError, BrowserSession
from app.services.submitters.base import (
    BaseSubmitter,
    BlockedBySite,
    SubmitField,
    SubmitOutcome,
    coerce_option,
    detect_human_wall,
)

logger = logging.getLogger(__name__)

# Stable ids Greenhouse uses for the fixed identity fields. Everything
# else is addressed by its API field name.
_FIXED_SELECTORS: dict[str, str] = {
    "first_name": "#first_name",
    "last_name": "#last_name",
    "email": "#email",
    "phone": "#phone",
    "resume": "input[type=file][id=resume]",
    "cover_letter": "#cover_letter_text",
}

# Confirmation copy Greenhouse shows after a successful submit. We
# require one of these (or a redirect to a confirmation URL) before
# recording the application as submitted — see SubmitOutcome.status.
_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for applying",
    "your application has been submitted",
    "application submitted",
    "thanks for applying",
)

_SUBMIT_SELECTOR = "#submit_app, button[type=submit], input[type=submit]"


class GreenhouseSubmitter(BaseSubmitter):
    platform = "greenhouse"

    async def submit(
        self,
        *,
        job_url: str,
        fields: list[SubmitField],
        resume_path: str | None = None,
        cover_letter_text: str | None = None,
        dry_run: bool = False,
    ) -> SubmitOutcome:
        unplaceable: list[str] = []
        issues: list[str] = []

        try:
            async with BrowserSession() as session:
                await session.navigate(job_url, wait_until="domcontentloaded")

                html = await session.html()
                wall = detect_human_wall(html)
                if wall:
                    raise BlockedBySite(f"page requires a human: {wall}")

                for f in fields:
                    placed = await self._place(session, f)
                    if not placed:
                        unplaceable.append(f.field_key)

                if resume_path:
                    try:
                        await session.upload(_FIXED_SELECTORS["resume"], resume_path)
                    except BrowserError:
                        unplaceable.append("resume")

                if cover_letter_text:
                    try:
                        await session.fill(
                            _FIXED_SELECTORS["cover_letter"], cover_letter_text
                        )
                    except BrowserError:
                        # Cover letter is optional on most Greenhouse
                        # boards; note it but don't fail the submission.
                        issues.append("cover_letter_field_absent")

                # A required field we could not place means the form we
                # send would not say what the user approved. Abort before
                # the click rather than submit a partial application.
                required_missing = [
                    f.field_key
                    for f in fields
                    if f.required and f.field_key in unplaceable
                ]
                if required_missing:
                    return SubmitOutcome(
                        status="failed",
                        unplaceable_fields=unplaceable,
                        detected_issues=issues,
                        error=(
                            "required fields could not be located in the form: "
                            + ", ".join(required_missing)
                        ),
                    )

                if dry_run:
                    return SubmitOutcome(
                        status="submitted",
                        confirmation_text=None,
                        detected_issues=issues + ["dry_run"],
                        unplaceable_fields=unplaceable,
                    )

                await session.click(_SUBMIT_SELECTOR)

                confirmation = await self._await_confirmation(session)
                if confirmation is None:
                    # No positive evidence. Do NOT record this as
                    # submitted — a Greenhouse form that fails inline
                    # validation stays on the page and raises nothing.
                    return SubmitOutcome(
                        status="failed",
                        unplaceable_fields=unplaceable,
                        detected_issues=issues + ["no_confirmation"],
                        error="submitted the form but saw no confirmation from Greenhouse",
                    )

                return SubmitOutcome(
                    status="submitted",
                    confirmation_text=confirmation,
                    detected_issues=issues,
                    unplaceable_fields=unplaceable,
                )

        except BlockedBySite as exc:
            return SubmitOutcome(status="blocked", error=str(exc))
        except BrowserError as exc:
            return SubmitOutcome(
                status="failed",
                error=f"browser error: {exc}",
                unplaceable_fields=unplaceable,
                detected_issues=issues,
            )

    # ── internals ──────────────────────────────────────────────────

    async def _place(self, session: BrowserSession, f: SubmitField) -> bool:
        """Put one answer into the form. False when we couldn't."""
        selector = _FIXED_SELECTORS.get(f.field_key) or self._name_selector(f.field_key)
        try:
            if f.field_type in ("select", "multi_select"):
                option = coerce_option(f.value, f.options)
                if option is None:
                    # No confident mapping from the saved answer onto the
                    # ATS's allowed values. Report unplaceable instead of
                    # choosing the nearest option (F346).
                    return False
                await session.select(selector, option)
            else:
                await session.fill(selector, f.value)
            return True
        except BrowserError:
            return False

    @staticmethod
    def _name_selector(field_key: str) -> str:
        # Greenhouse custom questions carry their API field name in the
        # `name` attribute. Escape quotes so a malformed key can't break
        # out of the attribute selector.
        safe = field_key.replace('"', '\\"')
        return f'[name="{safe}"]'

    @staticmethod
    async def _await_confirmation(session: BrowserSession) -> str | None:
        try:
            html = (await session.html()).lower()
        except BrowserError:
            return None
        for marker in _CONFIRMATION_MARKERS:
            if marker in html:
                return marker
        try:
            url = (await session.url()).lower()
        except BrowserError:
            return None
        if "confirmation" in url or "thank" in url:
            return f"redirected to {url}"
        return None
