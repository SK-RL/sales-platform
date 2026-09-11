"""Teamtailor application submitter.

F381. Verified on {slug}.teamtailor.com/jobs/{id}/applications/new
(virtasant, clearroute). A Rails form, #job-application-form, no
captcha: text controls by ``name`` (candidate[first_name] … and custom
answers candidate[answers_attributes][N][text|number|range]), radio /
checkbox groups by name with <label for> options, the résumé at
#candidate_resume_remote_url (mounted by the uploader script after
load), consent checkboxes, and ``input[type=submit][name=commit]``.
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
    "first_name": "candidate[first_name]",
    "last_name": "candidate[last_name]",
    "email": "candidate[email]",
    "phone": "candidate[phone]",
    "address": "candidate[location][query]",
    "work_history": "candidate[work_history]",
    "cover_letter": "candidate[job_applications_attributes][0][cover_letter]",
    "privacy_consent": "candidate[consent_given]",
    "future_jobs_consent": "candidate[consent_given_future_jobs]",
}
_FORM_READY_SELECTOR = "#job-application-form input[name='candidate[email]']"
_RESUME_SELECTOR = "#candidate_resume_remote_url"
_SUBMIT_SELECTOR = "#job-application-form input[type=submit][name=commit], #job-application-form button[type=submit]"
_HYDRATION_SETTLE_SECONDS = 3.0
_CONFIRMATION_WAIT_SECONDS = 25

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for your application",
    "thanks for your application",
    "thank you for applying",
    "application has been submitted",
    "application received",
    "we have received your application",
)

_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const form = document.getElementById('job-application-form'); const out = {};
  if (!form) return out;
  for (const e of form.querySelectorAll('input, textarea, select')) {
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


class TeamtailorSubmitter(BaseSubmitter):
    platform = "teamtailor"

    async def submit(self, *, job_url: str, fields: list[SubmitField], resume_path: str | None = None,
                     cover_letter_text: str | None = None, dry_run: bool = False) -> SubmitOutcome:
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
                        await asyncio.sleep(1.5)
                    except BrowserError:
                        pass
                if cover_letter_text and not any(f.field_key == "cover_letter" for f in fields):
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
                                         error="submitted the form but saw no confirmation from Teamtailor")
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
        return u if u.endswith("/applications/new") else u + "/applications/new"

    @staticmethod
    def _dom_name(f: SubmitField) -> str:
        return _FIXED_NAMES.get(f.field_key, f.field_key)

    @classmethod
    def _sel(cls, key: str, kind: str = "") -> str:
        """Rails emits a hidden <input name=… value=0> before every
        checkbox, so a boolean must be addressed as the checkbox itself."""
        prefix = "input[type=checkbox]" if kind == "boolean" else ""
        return "#job-application-form %s[name=%s]" % (prefix, json.dumps(_FIXED_NAMES.get(key, key)))

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        sel = self._sel(f.field_key, f.field_type)
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
                wanted = [v.strip() for v in re.split(r"\s*[;|]\s*|,\s*(?=[A-Z])", f.value or "") if v.strip()] if f.field_type == "multi_select" else [f.value]
                chosen = [coerce_option(v, f.options) for v in wanted]
                chosen = [c for c in chosen if c]
                if not chosen:
                    return False
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const wants = %s;"
                    " const form = document.getElementById('job-application-form');"
                    " const sel = form.querySelector(%s);"
                    " if (sel && sel.tagName === 'SELECT') { const o = [...sel.options].find(o => norm(o.textContent) === wants[0]); if (!o) return false; sel.value = o.value; sel.dispatchEvent(new Event('change', {bubbles: true})); return true; }"
                    " let ok = 0; for (const r of form.querySelectorAll('input[name=' + %s + ']')) { if (r.type !== 'radio' && r.type !== 'checkbox') continue;"
                    "   const lab = r.id && form.querySelector('label[for=\"' + CSS.escape(r.id) + '\"]');"
                    "   const t = norm(lab ? lab.innerText : r.value);"
                    "   if (wants.includes(t)) { if (!r.checked) (lab || r).click(); ok++; } }"
                    " return ok === wants.length; })()"
                    % (json.dumps([c.strip().lower() for c in chosen]), json.dumps(sel.replace("#job-application-form ", "")), json.dumps(json.dumps(self._dom_name(f))))))
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
            wants = [coerce_option(v.strip(), f.options) for v in re.split(r"\s*[;|]\s*|,\s*(?=[A-Z])", f.value or "")] if f.field_type == "multi_select" else [coerce_option(f.value, f.options)]
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
            if "/applications/" in url and "/new" not in url and "thank" in html:
                return f"redirected to {url}"
            await asyncio.sleep(1)
        return None
