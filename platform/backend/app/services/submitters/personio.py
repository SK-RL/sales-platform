"""Personio application submitter.

F378. Verified on greenbone-ag.jobs.personio.com/job/{id}/apply. Next.js
form, no captcha, no shadow DOM in the form itself. Every control has a
stable ``name`` (first_name, last_name, email, phone,
salary_expectations, custom_attribute_{id}) with ``id="field-{name}"``;
native ``<select>`` questions whose first option is a placeholder;
documents as ``documents.cv`` / ``documents.cover-letter`` file inputs
behind "Add file" buttons (set_input_files works on them directly).
Submit is ``button:has-text("Submit Application")``. The page is
requested with ``?language=en`` so labels and confirmation copy are
English.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

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

_FIXED_NAMES: dict[str, str] = {
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email",
    "phone": "phone",
    "salary_expectations": "salary_expectations",
    "resume": "documents.cv",
    "cover_letter_file": "documents.cover-letter",
}
_FORM_READY_SELECTOR = "input[name='email']"
_RESUME_SELECTOR = "input[type=file][name='documents.cv']"
_SUBMIT_SELECTOR = 'button:has-text("Submit Application"), button:has-text("Submit application")'
_HYDRATION_SETTLE_SECONDS = 2.0
_CONFIRMATION_WAIT_SECONDS = 20

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for your application",
    "thank you for applying",
    "application has been submitted",
    "application was submitted",
    "successfully submitted",
    "we have received your application",
    "vielen dank für deine bewerbung",
    "vielen dank für ihre bewerbung",
)

_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const e of document.querySelectorAll('input, textarea, select')) {
    if (!e.name || e.type === 'hidden' || e.type === 'file') continue;
    if (e.type === 'checkbox' || e.type === 'radio') out[e.name] = e.checked ? (norm(e.labels?.[0]?.innerText) || 'Yes') : (out[e.name] || '');
    else if (e.tagName === 'SELECT') out[e.name] = norm(e.selectedOptions[0]?.textContent || '');
    else out[e.name] = e.value || '';
  }
  return out;
})()
"""


class PersonioSubmitter(BaseSubmitter):
    platform = "personio"

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
                await session.navigate(self._apply_url(job_url), wait_until="domcontentloaded",
                                       wait_for_selector=_FORM_READY_SELECTOR)
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
                    issues.append("cover_letter_field_absent")

                for f in fields:
                    if not await self._place(session, f, resume_uploaded):
                        unplaceable.append(f.field_key)

                await asyncio.sleep(0.5)
                actual = await self._readback(session)
                for f in fields:
                    if f.field_key in unplaceable or f.field_type == "file":
                        continue
                    if not self._value_present(f, str(actual.get(self._dom_name(f), ""))):
                        unplaceable.append(f.field_key)
                        issues.append(f"unverified:{f.field_key}")

                placed_groups = {f.alternative_group for f in fields if f.alternative_group and f.field_key not in unplaceable}
                required_missing = [f.field_key for f in fields
                                    if f.required and f.field_key in unplaceable and f.alternative_group not in placed_groups]
                if required_missing:
                    return SubmitOutcome(status="failed", unplaceable_fields=unplaceable, detected_issues=issues,
                                         error="required fields could not be placed: " + ", ".join(required_missing))
                if dry_run:
                    return SubmitOutcome(status="submitted", detected_issues=issues + ["dry_run"], unplaceable_fields=unplaceable)

                await session.click(_SUBMIT_SELECTOR)
                confirmation = await self._await_confirmation(session)
                if confirmation is None:
                    return SubmitOutcome(status="failed", unplaceable_fields=unplaceable,
                                         detected_issues=issues + ["no_confirmation"],
                                         error="submitted the form but saw no confirmation from Personio")
                return SubmitOutcome(status="submitted", confirmation_text=confirmation, detected_issues=issues,
                                     unplaceable_fields=unplaceable)
        except BlockedBySite as exc:
            return SubmitOutcome(status="blocked", error=str(exc))
        except BrowserError as exc:
            return SubmitOutcome(status="failed", error=f"browser error: {exc}", unplaceable_fields=unplaceable,
                                 detected_issues=issues)

    @staticmethod
    def _apply_url(job_url: str) -> str:
        u = (job_url or "").split("?", 1)[0].rstrip("/")
        if not u.endswith("/apply"):
            u += "/apply"
        return u + "?language=en"

    @staticmethod
    def _dom_name(f: SubmitField) -> str:
        return _FIXED_NAMES.get(f.field_key, f.field_key)

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        sel = "[name=%s]" % json.dumps(self._dom_name(f))
        try:
            if f.field_type == "boolean":
                option = coerce_option(f.value, ["Yes", "No"])
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const e = document.querySelector(%s); if (!e) return false; const want = %s;"
                    " if (e.checked !== want) e.click(); return e.checked === want; })()" % (json.dumps(sel), "true" if option == "Yes" else "false")))
            if f.field_type in ("select", "multi_select"):
                option = coerce_option(f.value, f.options)
                if option is None:
                    return False
                # React listens to the native change event on a controlled
                # select; set via the prototype setter so React's tracker
                # sees the new value, then dispatch.
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const want = %s;"
                    " const sel = document.querySelector(%s); if (!sel || sel.tagName !== 'SELECT') return false;"
                    " const o = [...sel.options].find(o => norm(o.textContent) === want); if (!o) return false;"
                    " const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;"
                    " setter.call(sel, o.value); sel.dispatchEvent(new Event('change', {bubbles: true})); return true; })()"
                    % (json.dumps(option.strip().lower()), json.dumps(sel))))
            await session.fill(sel, f.value)
            return True
        except BrowserError:
            return False

    async def _readback(self, session: BrowserSession) -> dict:
        try:
            return await session.eval_js(_READBACK_JS) or {}
        except BrowserError:
            return {}

    @staticmethod
    def _value_present(f: SubmitField, got: str) -> bool:
        got_n = (got or "").strip().lower()
        if not got_n:
            return False
        if f.field_type == "boolean":
            wanted = (coerce_option(f.value, ["Yes", "No"]) or "").lower()
            return wanted == "no" or bool(got_n)
        if f.field_type in ("select", "multi_select"):
            w = (coerce_option(f.value, f.options) or f.value or "").strip().lower()
            return bool(w) and got_n == w
        wanted = (f.value or "").strip().lower()
        digits = "".join(c for c in wanted if c.isdigit())
        if len(digits) >= 7 and re.fullmatch(r"[\d+\-() .]+", wanted):
            gd = "".join(c for c in got_n if c.isdigit())
            return bool(gd) and (digits.endswith(gd) or gd.endswith(digits))
        return wanted[:24] in got_n

    @staticmethod
    async def _await_confirmation(session: BrowserSession) -> str | None:
        for _ in range(_CONFIRMATION_WAIT_SECONDS):
            try:
                html = (await session.html()).lower()
                url = (await session.url()).lower()
            except BrowserError:
                return None
            for m in _CONFIRMATION_MARKERS:
                if m in html:
                    return m
            if "/success" in url or "thank" in url:
                return f"redirected to {url}"
            await asyncio.sleep(1)
        return None
