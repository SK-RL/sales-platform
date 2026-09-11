"""Breezy HR application submitter.

F377. Verified against a live posting (vetsez.breezy.hr/p/18df3ec23bf901
…/apply). Facts the form settled:

* **Angular, single page, no captcha, no shadow DOM.** Every control is
  a plain input/select/radio/checkbox addressed by ``name``; fixed
  fields are ``cName``, ``cEmail``, ``cPhoneNumber``, ``cSalary`` (plus an
  unnamed period ``<select>`` that follows it), ``cResume``,
  ``smsConsent``, ``ccpaAgreement``. Custom questions are
  ``section_{id}_question_{n}``; EEO questions are radio groups whose
  options are ``li.option label[for]``.
* **A honeypot** — a text input named ``hp_*`` inside
  ``.apply-field-extra``. The extractor drops it, so no field here ever
  addresses it, and nothing is typed into it.
* **Native selects** commit on ``change``; Angular's model updates from
  that event, so options are set by matching text and dispatching it.
* **Submit is ``<button type="button">Submit Application</button>``**
  (not a submit input), addressed by text.
* **Confirmation copy**, from Breezy's own bundle:
  "Your application has been submitted successfully. Good luck!"
"""

from __future__ import annotations

import asyncio
import json
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

_FIXED_NAMES: dict[str, str] = {
    "name": "cName",
    "email": "cEmail",
    "phone": "cPhoneNumber",
    "salary": "cSalary",
    "resume": "cResume",
    "sms_consent": "smsConsent",
    "privacy_consent": "ccpaAgreement",
}
_FORM_READY_SELECTOR = "input[name='cName']"
_RESUME_SELECTOR = "input[type=file][name='cResume']"
_SUBMIT_SELECTOR = 'form button:has-text("Submit Application")'
_HYDRATION_SETTLE_SECONDS = 2.0
_CONFIRMATION_WAIT_SECONDS = 20

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "your application has been submitted successfully",
    "application has been submitted",
    "thank you for applying",
    "thanks for applying",
)

# Stamp the salary-period select (it has no name) so it can be addressed.
_STAMP_PERIOD_JS = """
(() => { const s = document.querySelector('[name="cSalary"]'); if (!s) return false;
  const sec = s.closest('.section') || document; let found = null;
  for (const el of sec.querySelectorAll('select')) { if (!el.name) { found = el; break; } }
  if (!found) return false; found.setAttribute('data-apply-field', 'salary_period'); return true; })()
"""

_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const e of document.querySelectorAll('form input, form textarea, form select')) {
    const key = e.getAttribute('data-apply-field') || e.name;
    if (!key || e.type === 'hidden' || e.type === 'file') continue;
    if (e.type === 'radio') {
      if (e.checked) { const lab = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`); out[key] = norm(lab ? lab.innerText : e.value); }
      else if (!(key in out)) out[key] = '';
    } else if (e.type === 'checkbox') {
      out[key] = e.checked ? 'Yes' : '';
    } else if (e.tagName === 'SELECT') {
      out[key] = norm(e.selectedOptions[0]?.textContent || '');
    } else {
      out[key] = e.value || '';
    }
  }
  return out;
})()
"""


class BreezySubmitter(BaseSubmitter):
    platform = "breezy"

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
                    issues.append("cover_letter_field_absent")

                try:
                    await session.eval_js(_STAMP_PERIOD_JS)
                except BrowserError:
                    pass

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

                placed_groups = {
                    f.alternative_group for f in fields
                    if f.alternative_group and f.field_key not in unplaceable
                }
                required_missing = [
                    f.field_key for f in fields
                    if f.required and f.field_key in unplaceable and f.alternative_group not in placed_groups
                ]
                if required_missing:
                    return SubmitOutcome(
                        status="failed", unplaceable_fields=unplaceable, detected_issues=issues,
                        error="required fields could not be placed: " + ", ".join(required_missing),
                    )

                if dry_run:
                    return SubmitOutcome(status="submitted", detected_issues=issues + ["dry_run"],
                                         unplaceable_fields=unplaceable)

                await session.click(_SUBMIT_SELECTOR)
                confirmation = await self._await_confirmation(session)
                if confirmation is None:
                    return SubmitOutcome(
                        status="failed", unplaceable_fields=unplaceable,
                        detected_issues=issues + ["no_confirmation"],
                        error="submitted the form but saw no confirmation from Breezy",
                    )
                return SubmitOutcome(status="submitted", confirmation_text=confirmation,
                                     detected_issues=issues, unplaceable_fields=unplaceable)

        except BlockedBySite as exc:
            return SubmitOutcome(status="blocked", error=str(exc))
        except BrowserError as exc:
            return SubmitOutcome(status="failed", error=f"browser error: {exc}",
                                 unplaceable_fields=unplaceable, detected_issues=issues)

    # ── internals ──────────────────────────────────────────────────

    @staticmethod
    def _apply_url(job_url: str) -> str:
        u = (job_url or "").split("?", 1)[0].rstrip("/")
        return u if u.endswith("/apply") else u + "/apply"

    @staticmethod
    def _dom_name(f: SubmitField) -> str:
        return _FIXED_NAMES.get(f.field_key, f.field_key)

    @staticmethod
    def _selector(f: SubmitField) -> str:
        if f.field_key == "salary_period":
            return '[data-apply-field="salary_period"]'
        return '[name=%s]' % json.dumps(_FIXED_NAMES.get(f.field_key, f.field_key))

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        sel = self._selector(f)
        try:
            if f.field_type == "boolean":
                option = coerce_option(f.value, ["Yes", "No"])
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const e = document.querySelector(%s); if (!e) return false;"
                    " const want = %s; if (e.checked !== want) { (e.labels && e.labels[0] ? e.labels[0] : e).click(); }"
                    " return e.checked === want; })()" % (json.dumps(sel), "true" if option == "Yes" else "false")
                ))
            if f.field_type in ("select", "multi_select"):
                option = coerce_option(f.value, f.options)
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const want = %s;"
                    " const sel = document.querySelector(%s);"
                    " if (sel && sel.tagName === 'SELECT') { const o = [...sel.options].find(o => norm(o.textContent) === want);"
                    "   if (!o) return false; sel.value = o.value; sel.dispatchEvent(new Event('change', {bubbles: true})); return true; }"
                    " for (const r of document.querySelectorAll('input[type=radio]' + %s)) {"
                    "   const lab = r.id && document.querySelector('label[for=\"' + CSS.escape(r.id) + '\"]');"
                    "   if (norm(lab ? lab.innerText : r.value) === want) { (lab || r).click(); return true; } }"
                    " return false; })()" % (json.dumps(option.strip().lower()), json.dumps(sel), json.dumps(sel))
                ))
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
            return wanted == "no" or got_n == "yes"
        if f.field_type in ("select", "multi_select"):
            w = (coerce_option(f.value, f.options) or f.value or "").strip().lower()
            return bool(w) and got_n == w
        wanted = (f.value or "").strip().lower()
        digits = "".join(c for c in wanted if c.isdigit())
        import re as _re
        if len(digits) >= 7 and _re.fullmatch(r"[\d+\-() .]+", wanted):
            gd = "".join(c for c in got_n if c.isdigit())
            return bool(gd) and (digits.endswith(gd) or gd.endswith(digits))
        return wanted[:24] in got_n

    @staticmethod
    async def _await_confirmation(session: BrowserSession) -> str | None:
        for _ in range(_CONFIRMATION_WAIT_SECONDS):
            try:
                html = (await session.html()).lower()
            except BrowserError:
                return None
            for m in _CONFIRMATION_MARKERS:
                if m in html:
                    return m
            await asyncio.sleep(1)
        return None
