"""Dover application submitter.

F389. Verified on app.dover.com/apply/{slug}/{uuid} (dover, lexoga,
mandrel): a React (MUI) form. Fixed fields by ``name`` — firstName,
lastName, email, linkedinUrl, phoneNumber; custom questions by ``name``
= the question's uuid (radios with ``value`` = option text, textareas
and inputs for answers); the résumé is the file input next to the
"Upload file" button; submit is the ``Apply`` button. Cloudflare
Turnstile runs invisibly (only a hidden ``cf-turnstile-response`` input
in the DOM, no widget) — as with Rippling nothing here touches it, and
a rendered challenge would stop the run via ``detect_human_wall``.
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
    "first_name": "firstName",
    "last_name": "lastName",
    "email": "email",
    "linkedin": "linkedinUrl",
    "phone": "phoneNumber",
}
_FORM_READY_SELECTOR = "form input[name=firstName]"
_RESUME_SELECTOR = "form input[type=file]"
_SUBMIT_SELECTOR = 'form button[type=submit]:has-text("Apply")'
_CONFIRMATION_WAIT_SECONDS = 25

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "application submitted",
    "application has been submitted",
    "we have received your application",
    "your application was submitted",
)

_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const form = document.querySelector('form'); const out = {};
  if (!form) return out;
  for (const e of form.querySelectorAll('input, textarea, select')) {
    if (!e.name || e.type === 'hidden' || e.type === 'file' || e.type === 'submit') continue;
    if (e.type === 'radio' || e.type === 'checkbox') {
      const lab = e.closest('label'); const text = norm(lab ? lab.innerText : e.value) || norm(e.value);
      if (e.checked) out[e.name] = out[e.name] ? out[e.name] + ' | ' + text : text;
      else if (!(e.name in out)) out[e.name] = '';
    } else if (e.tagName === 'SELECT') out[e.name] = norm(e.selectedOptions[0]?.textContent || '');
    else out[e.name] = e.value || '';
  }
  return out;
})()
"""


class DoverSubmitter(BaseSubmitter):
    platform = "dover"

    async def submit(self, *, job_url: str, fields: list[SubmitField], resume_path: str | None = None,
                     cover_letter_text: str | None = None, dry_run: bool = False) -> SubmitOutcome:
        unplaceable: list[str] = []
        issues: list[str] = []
        try:
            async with BrowserSession() as session:
                await session.navigate(job_url.split("?", 1)[0], wait_until="domcontentloaded",
                                       wait_for_selector=_FORM_READY_SELECTOR)
                await asyncio.sleep(3.0)
                wall = detect_human_wall(await session.html())
                if wall:
                    raise BlockedBySite(f"page requires a human: {wall}")

                resume_uploaded = False
                if resume_path:
                    try:
                        await session.upload(_RESUME_SELECTOR, resume_path)
                        resume_uploaded = True
                        await asyncio.sleep(2.5)
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
                                         error="submitted the form but saw no confirmation from Dover")
                return SubmitOutcome(status="submitted", confirmation_text=confirmation, detected_issues=issues,
                                     unplaceable_fields=unplaceable)
        except BlockedBySite as exc:
            return SubmitOutcome(status="blocked", error=str(exc))
        except BrowserError as exc:
            return SubmitOutcome(status="failed", error=f"browser error: {exc}", unplaceable_fields=unplaceable,
                                 detected_issues=issues)

    @staticmethod
    def _dom_name(f: SubmitField) -> str:
        return _FIXED_NAMES.get(f.field_key, f.field_key)

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        name = self._dom_name(f)
        sel = "form [name=%s]" % json.dumps(name)
        try:
            if f.field_type in ("select", "multi_select", "boolean"):
                options = f.options or (["Yes", "No"] if f.field_type == "boolean" else [])
                wanted = [v.strip() for v in re.split(r"\s*[;|]\s*", f.value or "") if v.strip()] if f.field_type == "multi_select" else [f.value]
                chosen = [c for c in (coerce_option(v, options) for v in wanted) if c]
                if not chosen:
                    return False
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const wants = %s;"
                    " const form = document.querySelector('form'); const sel = form.querySelector('select[name=' + %s + ']');"
                    " if (sel) { const o = [...sel.options].find(o => norm(o.textContent) === wants[0]); if (!o) return false;"
                    "   Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(sel, o.value); sel.dispatchEvent(new Event('change', {bubbles: true})); return true; }"
                    " let ok = 0; for (const r of form.querySelectorAll('input[name=' + %s + ']')) { if (r.type !== 'radio' && r.type !== 'checkbox') continue;"
                    "   const lab = r.closest('label'); const t = norm(lab ? lab.innerText : '') || norm(r.value);"
                    "   if (wants.includes(t) || wants.includes(norm(r.value))) { if (!r.checked) (lab || r).click(); ok++; } }"
                    " return ok === wants.length; })()"
                    % (json.dumps([c.strip().lower() for c in chosen]), json.dumps(json.dumps(name)), json.dumps(json.dumps(name)))))
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
        if f.field_type in ("select", "multi_select", "boolean"):
            options = f.options or (["Yes", "No"] if f.field_type == "boolean" else [])
            wants = [coerce_option(v.strip(), options) for v in re.split(r"\s*[;|]\s*", f.value or "")] if f.field_type == "multi_select" else [coerce_option(f.value, options)]
            wants = [w.strip().lower() for w in wants if w]
            return bool(wants) and all(w in got_n for w in wants)
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
            if "/apply/" not in url and ("thank" in html or "success" in url):
                return f"redirected to {url}"
            await asyncio.sleep(1)
        return None
