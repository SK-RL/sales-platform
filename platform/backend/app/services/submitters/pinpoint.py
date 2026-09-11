"""Pinpoint application submitter.

F387. Verified on {slug}.pinpointhq.com/en/postings/{uuid} (made-tech,
coforma). The posting page carries an "Apply" control; clicking it
reveals a Rails form (no captcha seen) whose controls are addressed by
``name``: ``application_form[application][first_name|last_name|email|
phone|address1|town|postcode|summary]``, the résumé at
``application_form[application][cv]``, and screening questions as
``application_form[application][answers_attributes][N][boolean_answer]``
(Yes/No radios, options addressed by <label for>) or
``[text_answer]`` (textarea). Select questions, Country / State and the
equality-monitoring block are React dropdowns; their nameless native
fallback <select> drives the React state when set through React's value
setter, and is what we read back (verified on made-tech: the hidden
``[country]`` / ``answer_options_attributes`` values post). Any other
enhanced dropdown (``pinpoint_*``) is reported unplaceable so the gate
stops for the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from app.fetchers.questions import _PINPOINT_SELECT_KEY_JS
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

_P = "application_form[application]"
_FIXED_NAMES: dict[str, str] = {
    "first_name": f"{_P}[first_name]",
    "last_name": f"{_P}[last_name]",
    "preferred_name": f"{_P}[preferred_name]",
    "email": f"{_P}[email]",
    "phone": f"{_P}[phone]",
    "address": f"{_P}[address1]",
    "city": f"{_P}[town]",
    "postcode": f"{_P}[postcode]",
    "summary": f"{_P}[summary]",
    "cover_letter": f"{_P}[summary]",
    "resume": f"{_P}[cv]",
}
_SELECT_PLACE_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const form = document.querySelector('form'); if (!form) return false;
  """ + _PINPOINT_SELECT_KEY_JS + r"""
  const key = %s, want = %s;
  const sel = [...form.querySelectorAll('select')].find(s => (s.name === key) || (!s.name && keyForSelect(s) === key));
  if (!sel) return false;
  const o = [...sel.options].find(o => norm(o.textContent) === want);
  if (!o) return false;
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
  setter.call(sel, o.value);
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return norm(sel.selectedOptions[0]?.textContent || '') === want;
})()
"""
_APPLY_SELECTOR = 'a:has-text("Apply"), button:has-text("Apply")'
_FORM_READY_SELECTOR = f"form input[name='{_P}[email]']"
_RESUME_SELECTOR = f"form input[type=file][name='{_P}[cv]']"
_SUBMIT_SELECTOR = 'form button:has-text("Submit Application"), form input[type=submit], form button[type=submit]'
_HYDRATION_SETTLE_SECONDS = 2.5
_CONFIRMATION_WAIT_SECONDS = 25

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for your application",
    "thanks for your application",
    "thank you for applying",
    "application has been submitted",
    "application received",
    "we have received your application",
    "application submitted",
)

_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const form = document.querySelector('form'); const out = {};
  if (!form) return out;
  """ + _PINPOINT_SELECT_KEY_JS + r"""
  for (const e of form.querySelectorAll('input, textarea, select')) {
    if (e.tagName === 'SELECT' && !e.name) { const k = keyForSelect(e); if (k) out[k] = norm(e.selectedOptions[0]?.textContent || ''); continue; }
    if (!e.name || e.type === 'hidden' || e.type === 'file' || e.type === 'submit') continue;
    if (e.type === 'radio' || e.type === 'checkbox') {
      const lab = e.id && form.querySelector(`label[for="${CSS.escape(e.id)}"]`);
      const text = norm(lab ? lab.innerText : e.value);
      if (e.checked) out[e.name] = out[e.name] ? out[e.name] + ' | ' + text : text;
      else if (!(e.name in out)) out[e.name] = '';
    } else if (e.tagName === 'SELECT') out[e.name] = norm(e.selectedOptions[0]?.textContent || '');
    else out[e.name] = e.value || '';
  }
  return out;
})()
"""


class PinpointSubmitter(BaseSubmitter):
    platform = "pinpoint"

    async def submit(self, *, job_url: str, fields: list[SubmitField], resume_path: str | None = None,
                     cover_letter_text: str | None = None, dry_run: bool = False) -> SubmitOutcome:
        unplaceable: list[str] = []
        issues: list[str] = []
        try:
            async with BrowserSession() as session:
                await session.navigate(job_url.split("?", 1)[0], wait_until="domcontentloaded", wait_for_selector="body")
                await asyncio.sleep(1.5)
                wall = detect_human_wall(await session.html())
                if wall:
                    raise BlockedBySite(f"page requires a human: {wall}")
                await self._reveal_form(session)
                wall = detect_human_wall(await session.html())
                if wall:
                    raise BlockedBySite(f"form requires a human: {wall}")

                resume_uploaded = False
                if resume_path:
                    try:
                        await session.upload(_RESUME_SELECTOR, resume_path)
                        resume_uploaded = True
                        await asyncio.sleep(1.0)
                    except BrowserError:
                        pass
                if cover_letter_text and not any(f.field_key in ("cover_letter", "summary") for f in fields):
                    try:
                        await session.fill(self._sel("cover_letter"), cover_letter_text)
                    except BrowserError:
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
                                         error="submitted the form but saw no confirmation from Pinpoint")
                return SubmitOutcome(status="submitted", confirmation_text=confirmation, detected_issues=issues,
                                     unplaceable_fields=unplaceable)
        except BlockedBySite as exc:
            return SubmitOutcome(status="blocked", error=str(exc))
        except BrowserError as exc:
            return SubmitOutcome(status="failed", error=f"browser error: {exc}", unplaceable_fields=unplaceable,
                                 detected_issues=issues)

    @staticmethod
    async def _reveal_form(session: BrowserSession) -> None:
        """The form is behind the posting page's Apply control; some
        tenants render it inline instead, so a missing control is fine."""
        if await session.eval_js("!!document.querySelector(%s)" % json.dumps(_FORM_READY_SELECTOR)):
            return
        try:
            await session.click(_APPLY_SELECTOR)
        except BrowserError:
            pass
        for _ in range(int(8 / 0.5)):
            await asyncio.sleep(0.5)
            if await session.eval_js("!!document.querySelector(%s)" % json.dumps(_FORM_READY_SELECTOR)):
                await asyncio.sleep(_HYDRATION_SETTLE_SECONDS)
                return
        raise BrowserError("Pinpoint application form did not appear after clicking Apply")

    @staticmethod
    def _dom_name(f: SubmitField) -> str:
        return _FIXED_NAMES.get(f.field_key, f.field_key)

    @classmethod
    def _sel(cls, key: str) -> str:
        return "form [name=%s]" % json.dumps(_FIXED_NAMES.get(key, key))

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        if f.field_key.startswith("pinpoint_"):
            return False  # enhanced dropdown with no addressable control
        name = self._dom_name(f)
        try:
            if f.field_type == "boolean":
                option = coerce_option(f.value, ["Yes", "No"])
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const want = %s;"
                    " const form = document.querySelector('form'); let hit = null;"
                    " for (const r of form.querySelectorAll('input[name=' + %s + ']')) {"
                    "   if (r.type === 'checkbox') { hit = r; if (r.checked !== (want === 'yes')) { const lab = r.id && form.querySelector('label[for=\"' + CSS.escape(r.id) + '\"]'); (lab || r).click(); } return r.checked === (want === 'yes'); }"
                    "   if (r.type !== 'radio') continue;"
                    "   const lab = r.id && form.querySelector('label[for=\"' + CSS.escape(r.id) + '\"]');"
                    "   const t = norm(lab ? lab.innerText : r.value);"
                    "   const v = norm(r.value);"
                    "   if (t === want || (want === 'yes' && (v === 'true' || v === '1')) || (want === 'no' && (v === 'false' || v === '0'))) { if (!r.checked) (lab || r).click(); hit = r; } }"
                    " return !!hit && hit.checked; })()" % (json.dumps(option.lower()), json.dumps(json.dumps(name)))))
            if f.field_type in ("select", "multi_select"):
                chosen = coerce_option(f.value, f.options)
                if not chosen:
                    return False
                if await session.eval_js(_SELECT_PLACE_JS % (json.dumps(name), json.dumps(chosen.strip().lower()))):
                    return True
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const want = %s;"
                    " const form = document.querySelector('form');"
                    " for (const r of form.querySelectorAll('input[name=' + %s + ']')) { if (r.type !== 'radio' && r.type !== 'checkbox') continue;"
                    "   const lab = r.id && form.querySelector('label[for=\"' + CSS.escape(r.id) + '\"]');"
                    "   if (norm(lab ? lab.innerText : r.value) === want) { if (!r.checked) (lab || r).click(); return true; } }"
                    " return false; })()" % (json.dumps(chosen.strip().lower()), json.dumps(json.dumps(name)))))
            await session.fill(self._sel(f.field_key), f.value)
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
            return wanted in got_n or got_n in ("true", "false", "1", "0")
        if f.field_type in ("select", "multi_select"):
            want = (coerce_option(f.value, f.options) or "").strip().lower()
            return bool(want) and want in got_n
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
            if "/applications/" in url or "/thank" in url or "confirmation" in url:
                return f"redirected to {url}"
            await asyncio.sleep(1)
        return None
