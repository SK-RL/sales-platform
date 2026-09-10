"""Greenhouse application submitter.

Verified against a live posting (job-boards.greenhouse.io, Figma
5426468004) rather than written from documentation. Four things that
inspection settled, each of which the first draft had wrong:

1. **Fields are addressed by ``id``, not ``name``.** The Job Board API
   returns field names like ``question_12497121004`` and the hosted
   board renders those as the DOM *id*. That page carried 22
   ``[id^="question_"]`` elements and **zero** ``[name^="question_"]``,
   so the original ``[name="..."]`` selector matched nothing and every
   custom question would have been unplaceable.
2. **There are no native ``<select>`` elements.** Every dropdown —
   including the EEO ones — is react-select: an ``<input role="combobox"
   class="select__input">``. ``select_option`` cannot drive those.
3. **There is no ``#submit_app``.** The control is
   ``<button type="submit">Submit application</button>``.
4. **reCAPTCHA Enterprise v3 is on every posting**, invisible and
   score-based. See ``base._HUMAN_REQUIRED_MARKERS`` — treating its
   presence as a wall would make this adapter a no-op.

The symmetry that matters still holds: ``field_key`` from
``fetchers/questions._fetch_greenhouse_questions`` is the same token the
DOM uses, so a field can never be shown to the user as answered and then
filled somewhere else.
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

# Verified live: modern boards have no `#submit_app`; the control is a
# `<button type="submit">Submit application</button>`. The id is kept
# first for older embedded boards that still render it.
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

                # Upload first: `_place` reports file fields as placed
                # based on this, which is what lets the alternative-group
                # logic below see that "Resume/CV" is satisfied.
                resume_uploaded = False
                if resume_path:
                    try:
                        await session.upload(_FIXED_SELECTORS["resume"], resume_path)
                        resume_uploaded = True
                    except BrowserError:
                        pass

                for f in fields:
                    placed = await self._place(
                        session, f, resume_uploaded=resume_uploaded
                    )
                    if not placed:
                        unplaceable.append(f.field_key)

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
                #
                # F350 — except when a sibling in the same alternative
                # group WAS placed. Greenhouse's "Resume/CV" question
                # offers `resume` (file) and `resume_text` (textarea) as
                # alternatives; the textarea isn't even rendered until
                # you pick "enter manually", so demanding both aborted
                # every application that attached a real resume.
                placed_groups = {
                    f.alternative_group
                    for f in fields
                    if f.alternative_group and f.field_key not in unplaceable
                }
                required_missing = [
                    f.field_key
                    for f in fields
                    if f.required
                    and f.field_key in unplaceable
                    and f.alternative_group not in placed_groups
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

    async def _place(
        self,
        session: BrowserSession,
        f: SubmitField,
        *,
        resume_uploaded: bool = False,
    ) -> bool:
        """Put one answer into the form. False when we couldn't."""
        if f.field_type == "file":
            # The adapter owns file handling: the resume went in via
            # `resume_path` above, and nothing else is uploadable. Report
            # it placed so alternative-group logic can see it.
            return f.field_key == "resume" and resume_uploaded

        selector = _FIXED_SELECTORS.get(f.field_key) or self._field_selector(f.field_key)
        try:
            if f.field_type in ("select", "multi_select"):
                option = coerce_option(f.value, f.options)
                if option is None:
                    # No confident mapping from the saved answer onto the
                    # ATS's allowed values. Report unplaceable instead of
                    # choosing the nearest option (F346).
                    return False
                return await self._place_choice(session, selector, option)
            await session.fill(selector, f.value)
            return True
        except BrowserError:
            return False

    async def _place_choice(
        self, session: BrowserSession, selector: str, option: str
    ) -> bool:
        """Choose ``option`` on a dropdown, native or react-select.

        Modern Greenhouse boards render **zero** native ``<select>``
        elements — verified on a live posting, where every dropdown
        (including the EEO ones) is an ``<input role="combobox"
        class="select__input">`` backed by react-select. ``select_option``
        cannot drive those, so we fall back to the interaction a person
        performs: focus, type the label, commit with Enter.

        Older embedded boards still use real selects, so we try that
        first and only fall through on failure.
        """
        try:
            await session.select(selector, option)
            return True
        except BrowserError:
            pass

        try:
            await session.click(selector)
            await session.fill(selector, option)
            await session.press(selector, "Enter")
        except BrowserError:
            return False

        # Verify rather than assume. react-select silently keeps the
        # field empty when the typed text matches no option, and an
        # unset required dropdown is exactly the silent-partial-submit
        # this adapter must never produce.
        return await self._choice_committed(session, selector)

    @staticmethod
    async def _choice_committed(session: BrowserSession, selector: str) -> bool:
        """True when react-select shows a committed value for the field."""
        try:
            # `.select__single-value` is react-select's rendered choice;
            # it only exists once an option is actually selected.
            container = f"{selector} ~ .select__single-value, {selector}"
            value = await session.attr(container, "value")
            if value:
                return True
            text = await session.text(".select__single-value")
            return bool(text and text.strip())
        except BrowserError:
            return False

    @staticmethod
    def _field_selector(field_key: str) -> str:
        """Address a custom question.

        Greenhouse's Job Board API returns field names like
        ``question_12497121004``, and on the hosted board those become
        the DOM **id**, not the ``name`` — verified live: a page with 22
        ``[id^="question_"]`` elements had **zero** ``[name^="question_"]``.
        The original ``[name="..."]`` selector therefore matched nothing
        and every custom question would have been unplaceable.

        We emit an id selector with a name-attribute fallback, since
        older embedded boards do still use ``name``.
        """
        safe = field_key.replace("\\", "\\\\").replace('"', '\\"')
        # CSS.escape equivalent for the id half: ids here are
        # alphanumeric + underscore in practice, but guard anyway.
        ident = "".join(c if (c.isalnum() or c in "_-") else f"\\{c}" for c in field_key)
        return f'#{ident}, [name="{safe}"]'

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
