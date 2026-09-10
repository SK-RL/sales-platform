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

import asyncio
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

# Anchor proving the application form is rendered, plus a settle for
# React hydration. See the note in submit() — filling before hydration
# silently discards the values.
_FORM_READY_SELECTOR = "#first_name"
_HYDRATION_SETTLE_SECONDS = 3.0

# Reads every identified input/textarea back out of the DOM. For a
# react-select combobox the typed text is cleared on commit and the
# chosen option renders in a sibling `.select__single-value`, so we
# look there when the input itself is empty.
_READBACK_JS = """
(() => {
  const out = {};
  document.querySelectorAll('input,textarea').forEach(e => {
    if (!e.id) return;
    let v = e.value || '';
    if (!v) {
      const ctrl = e.closest('.select__control');
      const sv = ctrl ? ctrl.querySelector('.select__single-value') : null;
      if (sv) v = sv.innerText.trim();
    }
    out[e.id] = v;
  });
  return out;
})()
"""


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
                # Wait for the form to exist AND for React to finish
                # hydrating before typing. Verified on a live posting:
                # filling at `domcontentloaded` silently lost the first
                # three fields (first_name, last_name, email) — hydration
                # replaced the inputs after we'd set them. Playwright
                # raised nothing and the fields read back empty, so the
                # adapter reported success on an application that would
                # have submitted with no name on it.
                await session.navigate(
                    job_url,
                    wait_until="domcontentloaded",
                    wait_for_selector=_FORM_READY_SELECTOR,
                )
                await asyncio.sleep(_HYDRATION_SETTLE_SECONDS)

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

                expected_display: dict[str, str] = {}
                for f in fields:
                    placed = await self._place(
                        session,
                        f,
                        resume_uploaded=resume_uploaded,
                        expected_display=expected_display,
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

                # Never trust `fill()`. Read the DOM back and demote any
                # field that doesn't actually hold what we intended —
                # this is the check that caught the hydration bug, and
                # the only thing standing between "we called fill" and
                # "the form says what the user approved".
                actual = await self._readback(session)
                for f in fields:
                    if f.field_key in unplaceable or f.field_type == "file":
                        continue
                    got = str(actual.get(f.field_key, ""))
                    if not self._value_present(f, got, expected_display):
                        unplaceable.append(f.field_key)
                        issues.append(f"unverified:{f.field_key}")

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
        expected_display: dict[str, str] | None = None,
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
                if expected_display is not None:
                    # What the ATS will *render* once committed. For
                    # Greenhouse's {"value","label"} options that's the
                    # label, not the value we submit, so verification has
                    # to compare against both.
                    expected_display[f.field_key] = self._display_for(option, f.options)
                return await self._place_choice(
                    session, selector, option, field_key=f.field_key
                )
            await session.fill(selector, f.value)
            return True
        except BrowserError:
            return False

    async def _place_choice(
        self, session: BrowserSession, selector: str, option: str,
        field_key: str = "",
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
        return await self._choice_committed(session, field_key)

    @staticmethod
    async def _choice_committed(session: BrowserSession, field_key: str) -> bool:
        """True when react-select shows a committed value for THIS field.

        The first version built a selector by interpolating an already
        comma-separated selector into another comma list, producing
        malformed CSS, and then fell back to reading the first
        `.select__single-value` anywhere on the page — so any *other*
        combobox holding a value made it return True. It reported every
        dropdown as placed regardless of what happened.
        """
        script = (
            "(() => { const e = document.getElementById(%r);"
            " if (!e) return '';"
            " const c = e.closest('.select__control');"
            " const sv = c ? c.querySelector('.select__single-value') : null;"
            " return (e.value || (sv ? sv.innerText.trim() : '')); })()"
            % field_key
        )
        try:
            return bool(await session.eval_js(script))
        except BrowserError:
            return False

    async def _readback(self, session: BrowserSession) -> dict:
        """Every identified field's current value, straight from the DOM."""
        try:
            return await session.eval_js(_READBACK_JS) or {}
        except BrowserError:
            return {}

    @staticmethod
    def _display_for(submitted: str, options: list) -> str:
        """The label the ATS renders for the option we chose."""
        for opt in options or []:
            if isinstance(opt, dict):
                if str(opt.get("value", "")) == submitted:
                    return str(opt.get("label") or submitted)
        return submitted

    @staticmethod
    def _value_present(f: SubmitField, got: str, expected_display: dict) -> bool:
        """Does the DOM actually hold what we meant to put there?"""
        got_n = (got or "").strip().lower()
        if not got_n:
            return False
        if f.field_type in ("select", "multi_select"):
            wanted = {
                (expected_display.get(f.field_key) or "").strip().lower(),
                (f.value or "").strip().lower(),
            }
            return any(w and w in got_n for w in wanted)
        # Text-ish: the value we typed must actually be there. Compare on
        # a prefix so trailing normalisation (trimming, phone masks)
        # doesn't produce false alarms.
        wanted = (f.value or "").strip().lower()
        return bool(wanted) and wanted[:24] in got_n

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
