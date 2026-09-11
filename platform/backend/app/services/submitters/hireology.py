"""Hireology application submitter.

F390. Verified on careers.hireology.com/{slug}/{id}/description
(familiarroadshomehealthcareagency): the application form is rendered
on the posting page itself, no captcha. Controls are addressed by id
``#{field}-0`` (text, email, tel, select, file ``#resume-0``, checkbox
``#sms_opt_in-0``); radio groups are ``input[name="{field}-0"]`` with
``<label for>`` options; submit is ``#submit`` ("SUBMIT YOUR
APPLICATION"). Field ids come from the public form schema the extractor
reads, mapped back here from our fixed keys.
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

_FIXED_IDS: dict[str, str] = {
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email_address",
    "phone": "home_phone",
    "address": "street_address",
    "city": "city",
    "state": "state_id",
    "postcode": "zip_code",
    "resume": "resume",
    "candidate_referred": "candidate_referred",
    "referred_by": "referred_by",
    "cover_letter": "cover_letter",
    "sms_opt_in": "sms_opt_in",
}
_FORM_READY_SELECTOR = "#first_name-0"
_APPLY_NOW_SELECTOR = "#apply-now-button"
_SUBMIT_SELECTOR = "#submit, button.careers-submit-button"
_CONFIRMATION_WAIT_SECONDS = 25

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for applying",
    "thank you for your application",
    "thanks for applying",
    "application has been submitted",
    "application has been received",
    "we have received your application",
    "application submitted",
)

_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const e of document.querySelectorAll('input, textarea, select')) {
    const m = (e.id || '').match(/^(.+?)(?:-(?:yes|no|[^-]+))?-0$/);
    if (e.type === 'hidden' || e.type === 'file' || e.type === 'submit') continue;
    if (e.type === 'radio') {
      const key = (e.name || '').replace(/-0$/, ''); if (!key) continue;
      const lab = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
      const text = norm(lab ? lab.innerText : e.value);
      if (e.checked) out[key] = text; else if (!(key in out)) out[key] = '';
      continue;
    }
    if (!m) continue;
    const key = (e.id || '').replace(/-0$/, '');
    if (e.type === 'checkbox') out[key] = e.checked ? 'Yes' : 'No';
    else if (e.tagName === 'SELECT') out[key] = norm(e.selectedOptions[0]?.textContent || '');
    else out[key] = e.value || '';
  }
  return out;
})()
"""


class HireologySubmitter(BaseSubmitter):
    platform = "hireology"

    async def submit(self, *, job_url: str, fields: list[SubmitField], resume_path: str | None = None,
                     cover_letter_text: str | None = None, dry_run: bool = False) -> SubmitOutcome:
        unplaceable: list[str] = []
        issues: list[str] = []
        try:
            async with BrowserSession() as session:
                await session.navigate(job_url.split("?", 1)[0], wait_until="domcontentloaded", wait_for_selector="body")
                await asyncio.sleep(2.0)
                wall = detect_human_wall(await session.html())
                if wall:
                    raise BlockedBySite(f"page requires a human: {wall}")
                await self._reveal_form(session)

                resume_uploaded = False
                if resume_path:
                    try:
                        await session.upload("#resume-0", resume_path)
                        resume_uploaded = True
                        await asyncio.sleep(1.5)
                    except BrowserError:
                        pass
                if cover_letter_text and not any(f.field_key == "cover_letter" for f in fields):
                    try:
                        await session.fill("#cover_letter-0", cover_letter_text)
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
                    if not self._value_present(f, str(actual.get(self._dom_id(f), ""))):
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
                                         error="submitted the form but saw no confirmation from Hireology")
                return SubmitOutcome(status="submitted", confirmation_text=confirmation, detected_issues=issues,
                                     unplaceable_fields=unplaceable)
        except BlockedBySite as exc:
            return SubmitOutcome(status="blocked", error=str(exc))
        except BrowserError as exc:
            return SubmitOutcome(status="failed", error=f"browser error: {exc}", unplaceable_fields=unplaceable,
                                 detected_issues=issues)

    @staticmethod
    async def _reveal_form(session: BrowserSession) -> None:
        """The form is on the posting page; some themes show it only
        after "Apply now"."""
        if await session.eval_js("!!document.querySelector(%s)" % json.dumps(_FORM_READY_SELECTOR)):
            return
        try:
            await session.click(_APPLY_NOW_SELECTOR)
        except BrowserError:
            pass
        for _ in range(16):
            await asyncio.sleep(0.5)
            if await session.eval_js("!!document.querySelector(%s)" % json.dumps(_FORM_READY_SELECTOR)):
                await asyncio.sleep(1.0)
                return
        raise BrowserError("Hireology application form did not appear")

    @staticmethod
    def _dom_id(f: SubmitField) -> str:
        return _FIXED_IDS.get(f.field_key, f.field_key)

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        fid = self._dom_id(f)
        sel = "#%s-0" % fid
        try:
            if f.field_type == "boolean":
                option = coerce_option(f.value, ["Yes", "No"])
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const e = document.querySelector(%s); if (!e || e.type !== 'checkbox') return false; const want = %s;"
                    " if (e.checked !== want) { const lab = document.querySelector('label[for=\"' + CSS.escape(e.id) + '\"]'); (lab || e).click(); }"
                    " return e.checked === want; })()" % (json.dumps(sel), "true" if option == "Yes" else "false")))
            if f.field_type in ("select", "multi_select"):
                chosen = coerce_option(f.value, f.options)
                if not chosen:
                    return False
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const want = %s;"
                    " const sel = document.querySelector(%s);"
                    " if (sel && sel.tagName === 'SELECT') { const o = [...sel.options].find(o => norm(o.textContent) === want); if (!o) return false; sel.value = o.value; sel.dispatchEvent(new Event('change', {bubbles: true})); return true; }"
                    " for (const r of document.querySelectorAll('input[type=radio][name=' + %s + ']')) {"
                    "   const lab = r.id && document.querySelector('label[for=\"' + CSS.escape(r.id) + '\"]');"
                    "   if (norm(lab ? lab.innerText : r.value) === want) { if (!r.checked) (lab || r).click(); return true; } }"
                    " return false; })()" % (json.dumps(chosen.strip().lower()), json.dumps(sel), json.dumps(json.dumps(fid + "-0")))))
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
            return got_n == wanted
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
            if "/description" not in url and ("thank" in html or "confirmation" in url or "success" in url):
                return f"redirected to {url}"
            await asyncio.sleep(1)
        return None
