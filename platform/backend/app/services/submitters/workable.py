"""Workable application submitter.

F365. Verified against two live boards (apply.workable.com/deeplight,
/payabl) rather than written from documentation. Workable is the most
automation-friendly ATS so far and the first whose form matched its own
description on first inspection:

* **Open to headless Chromium** — no DataDome (SmartRecruiters), no
  hCaptcha (Lever), no login wall. ~190 KB rendered document.
* **No shadow DOM, no native ``<select>``.** Every field is a plain
  input/textarea/radio wrapped in an element carrying ``data-ui``.
* **Fixed fields keyed by ``name``** — ``firstname``, ``lastname``,
  ``email``, ``phone``, ``headline``, ``address``, ``summary``,
  ``cover_letter``. The resume is ``[data-ui="resume"] input[type=file]``
  (there is a second file input for an avatar; scope by data-ui, never
  by ``input[type=file]`` alone).
* **Custom questions are ``QA_<id>``.** Free text is a ``<textarea>``
  whose id equals its name. Choices are radios sharing the ``QA_`` name,
  each option wrapped in ``[data-ui="option"]`` with a ``<label>``.
* **Submit is ``button[data-ui="apply-button"]``**, text "Submit
  application". Scoped by data-ui, not by ``button[type=submit]`` — the
  cookie-banner lesson from Recruitee.

As with every adapter: fills are verified by DOM readback, an
unplaceable *required* field aborts before the click, and only a
confirmation from Workable flips the outcome to ``submitted``.
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

# Our canonical keys -> Workable input names.
_FIXED_NAMES: dict[str, str] = {
    "first_name": "firstname",
    "last_name": "lastname",
    "email": "email",
    "phone": "phone",
    "headline": "headline",
    "address": "address",
    "city": "city",
    "postcode": "postcode",
    "country": "country",
    "summary": "summary",
    "cover_letter": "cover_letter",
}

# data-ui is on the INPUT itself, not a wrapper — verified live; the
# wrapper form matched nothing and the resume never uploaded.
_RESUME_SELECTOR = 'input[type=file][data-ui="resume"]'
_SUBMIT_SELECTOR = 'button[data-ui="apply-button"]'
_FORM_READY_SELECTOR = 'input[name="firstname"]'
_HYDRATION_SETTLE_SECONDS = 2.0

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for applying",
    "thanks for applying",
    "your application has been submitted",
    "application submitted",
    "we have received your application",
    "application received",
)

_READBACK_JS = """
(() => {
  const out = {};
  document.querySelectorAll('input,textarea').forEach(e => {
    if (!e.name || e.type === 'hidden' || e.type === 'file') return;
    if (e.type === 'radio' || e.type === 'checkbox') {
      if (e.checked) out[e.name] = (e.closest('label')?.innerText || e.value || '').trim();
      else if (!(e.name in out)) out[e.name] = '';
    } else {
      out[e.name] = e.value || '';
    }
  });
  return out;
})()
"""


class WorkableSubmitter(BaseSubmitter):
    platform = "workable"

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
                await session.navigate(
                    self._apply_url(job_url),
                    wait_until="domcontentloaded",
                    wait_for_selector=_FORM_READY_SELECTOR,
                )
                await asyncio.sleep(_HYDRATION_SETTLE_SECONDS)

                wall = detect_human_wall(await session.html())
                if wall:
                    raise BlockedBySite(f"page requires a human: {wall}")

                resume_uploaded = False
                if resume_path:
                    try:
                        await session.upload(_RESUME_SELECTOR, resume_path)
                        resume_uploaded = True
                    except BrowserError:
                        pass

                if cover_letter_text:
                    try:
                        await session.fill('[name="cover_letter"]', cover_letter_text)
                    except BrowserError:
                        issues.append("cover_letter_field_absent")

                for f in fields:
                    if not await self._place(session, f, resume_uploaded):
                        unplaceable.append(f.field_key)

                # Never trust fill(); read the DOM back. The settle matters:
                # this is a React form and a radio picked via label.click()
                # commits its checked state on the next tick. The last field
                # placed (QA_11260390 on deeplight) read back unchecked with
                # no settle — a race, not a failed click.
                await asyncio.sleep(0.5)
                actual = await self._readback(session)
                for f in fields:
                    if f.field_key in unplaceable or f.field_type == "file":
                        continue
                    got = str(actual.get(self._dom_name(f), ""))
                    if not self._value_present(f, got):
                        unplaceable.append(f.field_key)
                        issues.append(f"unverified:{f.field_key}")

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
                        error="required fields could not be placed: " + ", ".join(required_missing),
                    )

                if dry_run:
                    return SubmitOutcome(
                        status="submitted",
                        detected_issues=issues + ["dry_run"],
                        unplaceable_fields=unplaceable,
                    )

                await session.click(_SUBMIT_SELECTOR)
                confirmation = await self._await_confirmation(session)
                if confirmation is None:
                    return SubmitOutcome(
                        status="failed",
                        unplaceable_fields=unplaceable,
                        detected_issues=issues + ["no_confirmation"],
                        error="submitted the form but saw no confirmation from Workable",
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

    @staticmethod
    def _apply_url(job_url: str) -> str:
        """The scanner stores the posting page; the form lives at /apply/."""
        u = job_url.rstrip("/")
        return u if u.endswith("/apply") else u + "/apply/"

    @staticmethod
    def _dom_name(f: SubmitField) -> str:
        return _FIXED_NAMES.get(f.field_key, f.field_key)

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        name = self._dom_name(f)
        try:
            if f.field_type in ("select", "multi_select", "boolean"):
                option = (
                    coerce_option(f.value, ["Yes", "No"]) if f.field_type == "boolean"
                    else coerce_option(f.value, f.options)
                )
                if option is None:
                    return False
                # The radio is 13x13 at opacity:0 inside a <label> whose
                # text is the option, all under fieldset[data-ui="QA_…"].
                # Playwright's click times out on it (an overlay intercepts)
                # and check() reports "did not change its state" — the same
                # pattern Recruitee had. A JS label.click() fires the events
                # the framework listens for; the readback pass proves it
                # stuck, so this can't silently no-op.
                return await self._pick_radio(session, name, option)
            await session.fill(f'[name="{name}"]', f.value)
            return True
        except BrowserError:
            return False

    @staticmethod
    async def _pick_radio(session: BrowserSession, name: str, option: str) -> bool:
        import json as _json

        script = (
            "(() => { const grp = document.querySelector('fieldset[data-ui=' + %s + ']')"
            " || document.querySelector('input[name=' + %s + ']')?.closest('fieldset');"
            " if (!grp) return false;"
            " const want = %s.trim().toLowerCase();"
            " const lab = [...grp.querySelectorAll('label')].find(l => (l.innerText||'').trim().toLowerCase() === want);"
            " if (!lab) return false; lab.click(); return true; })()"
            % (_json.dumps(_json.dumps(name)), _json.dumps(_json.dumps(name)), _json.dumps(option))
        )
        try:
            return bool(await session.eval_js(script))
        except BrowserError:
            return False

    async def _readback(self, session: BrowserSession) -> dict:
        try:
            return await session.eval_js(_READBACK_JS) or {}
        except BrowserError:
            return {}

    @staticmethod
    def _esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _value_present(f: SubmitField, got: str) -> bool:
        got_n = (got or "").strip().lower()
        if not got_n:
            return False
        if f.field_type == "boolean":
            wanted = (coerce_option(f.value, ["Yes", "No"]) or "").lower()
            return bool(wanted) and wanted in got_n
        if f.field_type in ("select", "multi_select"):
            w = (f.value or "").strip().lower()
            return bool(w) and (w in got_n or got_n in w)
        wanted = (f.value or "").strip().lower()
        digits = "".join(c for c in wanted if c.isdigit())
        if digits and len(digits) >= len(wanted) - 3:
            gd = "".join(c for c in got_n if c.isdigit())
            return bool(gd) and (digits.endswith(gd) or gd.endswith(digits))
        return bool(wanted) and wanted[:24] in got_n

    @staticmethod
    async def _await_confirmation(session: BrowserSession) -> str | None:
        try:
            html = (await session.html()).lower()
        except BrowserError:
            return None
        for m in _CONFIRMATION_MARKERS:
            if m in html:
                return m
        try:
            url = (await session.url()).lower()
        except BrowserError:
            return None
        return f"redirected to {url}" if ("/thank" in url or "confirmation" in url) else None
