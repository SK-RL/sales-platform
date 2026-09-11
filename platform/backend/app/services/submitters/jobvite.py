"""Jobvite application submitter.

F388. Verified on jobs.jobvite.com/progress/job/{id}/apply: an
AngularJS form (``scopeData.applyForm``). Some tenants gate it with a
data-consent step — ``#jv-country-select`` + "I Accept" — whose
choice is the ``jv_consent_region`` question the user answers once.
Behind it: text inputs by ``name`` (``input-{id}``), radios by
``name`` (``{id}``) with <label for> options, native selects, the
résumé as the hidden ``#file-input-0`` (set_input_files works on it),
and ``button:has-text("Send Application")``. reCAPTCHA is present but
``size=invisible`` (score-based), so ``detect_human_wall`` passes it.
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

_CONSENT_SELECT = "#jv-country-select"
_CONSENT_ACCEPT = 'button:has-text("I Accept"), button:has-text("Accept")'
_RESUME_SELECTOR = "#file-input-0"
_SUBMIT_SELECTOR = 'button:has-text("Send Application"), button[type=submit].jv-button-primary'
_CONFIRMATION_WAIT_SECONDS = 25

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for applying",
    "thank you for your application",
    "thanks for applying",
    "application has been received",
    "application has been submitted",
    "we have received your application",
    "your application was sent",
)

_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const e of document.querySelectorAll('form input, form textarea, form select')) {
    if (!e.name || e.type === 'hidden' || e.type === 'file' || e.type === 'submit') continue;
    if (e.type === 'radio' || e.type === 'checkbox') {
      const lab = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
      const text = norm(lab ? lab.innerText : e.value);
      if (e.checked) out[e.name] = out[e.name] ? out[e.name] + ' | ' + text : text;
      else if (!(e.name in out)) out[e.name] = '';
    } else if (e.tagName === 'SELECT') out[e.name] = norm(e.selectedOptions[0]?.textContent || '');
    else out[e.name] = e.value || '';
  }
  return out;
})()
"""

_LABEL_TO_NAME_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim().replace(/\s*\*\s*$/, '').toLowerCase();
  const want = %s; 
  for (const g of document.querySelectorAll('form .jv-form-field')) {
    const q = g.querySelector('legend, label.jv-form-field-label, label');
    if (q && norm(q.innerText) === want) { const c = g.querySelector('input:not([type=hidden]), select, textarea'); return c ? c.name : ''; }
  }
  return '';
})()
"""


class JobviteSubmitter(BaseSubmitter):
    platform = "jobvite"

    async def submit(self, *, job_url: str, fields: list[SubmitField], resume_path: str | None = None,
                     cover_letter_text: str | None = None, dry_run: bool = False) -> SubmitOutcome:
        unplaceable: list[str] = []
        issues: list[str] = []
        try:
            async with BrowserSession() as session:
                await session.navigate(self._apply_url(job_url), wait_until="domcontentloaded", wait_for_selector="body")
                await asyncio.sleep(3.5)
                wall = detect_human_wall(await session.html())
                if wall:
                    raise BlockedBySite(f"page requires a human: {wall}")

                consent = next((f for f in fields if f.field_key == "jv_consent_region"), None)
                if await session.eval_js("!!document.querySelector(%s)" % json.dumps(_CONSENT_SELECT)):
                    if consent is None or not await self._pass_consent(session, consent):
                        unplaceable.append("jv_consent_region")
                        return SubmitOutcome(status="failed", unplaceable_fields=unplaceable, detected_issues=issues,
                                             error="required fields could not be placed: jv_consent_region")
                    wall = detect_human_wall(await session.html())
                    if wall:
                        raise BlockedBySite(f"form requires a human: {wall}")

                resume_uploaded = False
                if resume_path:
                    try:
                        await session.upload(_RESUME_SELECTOR, resume_path)
                        resume_uploaded = True
                        await asyncio.sleep(2.5)
                    except BrowserError:
                        pass
                if cover_letter_text:
                    issues.append("cover_letter_field_absent")  # Jobvite takes a cover letter only as a file

                names: dict[str, str] = {}
                for f in fields:
                    if f.field_key == "jv_consent_region":
                        continue
                    name = await self._dom_name(session, f)
                    names[f.field_key] = name
                    if not name or not await self._place(session, f, name, resume_uploaded):
                        unplaceable.append(f.field_key)

                await asyncio.sleep(0.5)
                actual = await self._readback(session)
                for f in fields:
                    if f.field_key in unplaceable or f.field_type == "file" or f.field_key == "jv_consent_region":
                        continue
                    if not self._value_present(f, str(actual.get(names.get(f.field_key, ""), ""))):
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
                                         error="submitted the form but saw no confirmation from Jobvite")
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
        return u if u.endswith("/apply") else u + "/apply"

    @staticmethod
    async def _pass_consent(session: BrowserSession, consent: SubmitField) -> bool:
        chosen = coerce_option(consent.value, consent.options)
        if not chosen:
            return False
        ok = await session.eval_js(
            "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const s = document.querySelector(%s);"
            " const o = [...s.options].find(o => norm(o.textContent) === %s); if (!o) return false;"
            " s.value = o.value; s.dispatchEvent(new Event('change', {bubbles: true})); return true; })()"
            % (json.dumps(_CONSENT_SELECT), json.dumps(chosen.strip().lower())))
        if not ok:
            return False
        await asyncio.sleep(1.5)
        try:
            await session.click(_CONSENT_ACCEPT)
        except BrowserError:
            return False
        for _ in range(16):
            await asyncio.sleep(0.5)
            if await session.eval_js("!document.querySelector(%s) && !!document.querySelector('form .jv-form-field')" % json.dumps(_CONSENT_SELECT)):
                await asyncio.sleep(2.0)
                return True
        return False

    @staticmethod
    async def _dom_name(session: BrowserSession, f: SubmitField) -> str:
        """Fixed keys (first_name …) were mapped from labels at extraction;
        map them back by label. Everything else is keyed by the control name."""
        if f.field_type == "file" or f.field_key.startswith("input-") or re.fullmatch(r"[A-Za-z0-9]{8}", f.field_key or ""):
            return f.field_key
        try:
            return str(await session.eval_js(_LABEL_TO_NAME_JS % json.dumps((f.label or "").strip().lower())) or "")
        except BrowserError:
            return ""

    async def _place(self, session: BrowserSession, f: SubmitField, name: str, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        sel = "form [name=%s]" % json.dumps(name)
        try:
            if f.field_type == "boolean":
                option = coerce_option(f.value, ["Yes", "No"])
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const e = document.querySelector(%s); if (!e) return false; const want = %s;"
                    " if (e.checked !== want) { const lab = e.id && document.querySelector('label[for=\"' + CSS.escape(e.id) + '\"]'); (lab || e).click(); }"
                    " return e.checked === want; })()" % (json.dumps(sel), "true" if option == "Yes" else "false")))
            if f.field_type in ("select", "multi_select"):
                wanted = [v.strip() for v in re.split(r"\s*[;|]\s*", f.value or "") if v.strip()] if f.field_type == "multi_select" else [f.value]
                chosen = [c for c in (coerce_option(v, f.options) for v in wanted) if c]
                if not chosen:
                    return False
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const wants = %s;"
                    " const sel = document.querySelector('form select[name=' + %s + ']');"
                    " if (sel) { const o = [...sel.options].find(o => norm(o.textContent) === wants[0]); if (!o) return false; sel.value = o.value; sel.dispatchEvent(new Event('change', {bubbles: true})); return true; }"
                    " let ok = 0; for (const r of document.querySelectorAll('form input[name=' + %s + ']')) { if (r.type !== 'radio' && r.type !== 'checkbox') continue;"
                    "   const lab = r.id && document.querySelector('label[for=\"' + CSS.escape(r.id) + '\"]');"
                    "   if (wants.includes(norm(lab ? lab.innerText : r.value))) { if (!r.checked) (lab || r).click(); ok++; } }"
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
        if f.field_type == "boolean":
            wanted = (coerce_option(f.value, ["Yes", "No"]) or "").lower()
            return wanted == "no" or bool(got_n)
        if f.field_type in ("select", "multi_select"):
            wants = [coerce_option(v.strip(), f.options) for v in re.split(r"\s*[;|]\s*", f.value or "")] if f.field_type == "multi_select" else [coerce_option(f.value, f.options)]
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
            if "/apply" not in url and ("thank" in html or "confirmation" in url):
                return f"redirected to {url}"
            await asyncio.sleep(1)
        return None
