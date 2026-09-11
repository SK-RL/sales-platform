"""Rippling ATS application submitter.

F379. Verified on ats.rippling.com/athennian/jobs/{uuid}/apply. React
form whose input ``name``s change per render, so nothing is addressed by
name: text inputs by ``[data-testid="input-{key}"]``; comboboxes
(pronouns, location) by their label, typed into and then picked from
the ``[role=option]`` list; custom questions are radio groups named
``customQuestions.{qid}.{optionId}`` chosen by clicking the
``[role=radio][data-value=…]`` wrapper; the résumé is the file input
inside ``[data-testid=resume]``. Submit is the ``Apply`` button.

Cloudflare Turnstile runs INVISIBLY here — the token is minted by
Cloudflare's script on submit, no widget is shown — so, as with Ashby's
reCAPTCHA v3, nothing here touches it and the outcome is whatever
Rippling reports. A visible challenge would show up in
``detect_human_wall`` (``cf-turnstile`` / ``challenges.cloudflare.com``
iframe) and stop the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from app.services.playwright_browser import BrowserError, BrowserSession
from app.services.submitters.ashby import choose_combobox_option
from app.services.submitters.base import (
    BaseSubmitter,
    BlockedBySite,
    SubmitField,
    SubmitOutcome,
    coerce_option,
    detect_human_wall,
)

logger = logging.getLogger(__name__)

_TESTIDS: dict[str, str] = {
    "first_name": "input-first_name",
    "last_name": "input-last_name",
    "email": "input-email",
    "phone": "input-phone_number",
    "current_company": "input-current_company",
    "linkedin_url": "input-linkedin_link",
}
_FORM_READY_SELECTOR = '[data-testid="input-email"]'
_RESUME_SELECTOR = '[data-testid="resume"] input[type=file]'
_SUBMIT_SELECTOR = 'button[type=submit]:has-text("Apply"), button:has-text("Submit application")'
_HYDRATION_SETTLE_SECONDS = 3.0
_COMBOBOX_SETTLE_SECONDS = 1.5
_CONFIRMATION_WAIT_SECONDS = 25

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "application submitted",
    "your application has been submitted",
    "thank you for applying",
    "thanks for applying",
    "we have received your application",
    "application received",
    "successfully submitted",
)

# Stamp a labelled combobox so it can be addressed by our key.
_STAMP_COMBOBOX_JS = """
(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const want = %s;
  for (const e of document.querySelectorAll('input[role=combobox], input[data-testid="input-undefined"]')) {
    const lb = (e.getAttribute('aria-labelledby')||'').split(/\\s+/).map(i => document.getElementById(i)?.innerText||'').join(' ');
    if (norm(lb) === want) { e.setAttribute('data-apply-field', %s); return true; } }
  return false; })()
"""

_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const e of document.querySelectorAll('input, textarea')) {
    if (e.type === 'hidden' || e.type === 'file') continue;
    const tid = e.getAttribute('data-testid') || '';
    const key = e.getAttribute('data-apply-field') || (tid.startsWith('input-') ? tid.slice(6) : '') || e.name;
    if (!key) continue;
    if (e.type === 'radio') {
      const rg = e.closest('[role=radiogroup]');
      const checked = rg ? rg.querySelector('[role=radio][aria-checked="true"]') : null;
      // The wrapper's data-value is the submitted value ("true"), the
      // label is what the candidate read ("Yes - I consent…"): report both
      // so verification can match either.
      out[e.name] = checked ? norm(checked.innerText) + ' | ' + (checked.getAttribute('data-value') || '') : (e.checked ? e.value : (out[e.name] || ''));
    } else {
      out[key] = e.value || '';
    }
  }
  return out;
})()
"""


class RipplingSubmitter(BaseSubmitter):
    platform = "rippling"

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
                        # Rippling parses the résumé and may autofill; let it settle
                        # before we type, so our values win.
                        await asyncio.sleep(2.0)
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
                    if not self._value_present(f, str(actual.get(self._dom_key(f), ""))):
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
                                         error="submitted the form but saw no confirmation from Rippling")
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
    def _dom_key(f: SubmitField) -> str:
        tid = _TESTIDS.get(f.field_key)
        if tid:
            return tid[len("input-"):]
        return f.field_key

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        try:
            if f.field_type in ("select", "multi_select", "boolean"):
                option = coerce_option(f.value, f.options or ["Yes", "No"])
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); const want = %s;"
                    " const rg = document.querySelector('[role=radiogroup][data-testid=' + %s + ']')"
                    "  || document.querySelector('input[type=radio][name=' + %s + ']')?.closest('[role=radiogroup]');"
                    " if (!rg) return false;"
                    " const r = [...rg.querySelectorAll('[role=radio]')].find(x => norm(x.getAttribute('data-value')) === want || norm(x.innerText) === want);"
                    " if (!r) return false; r.click(); return true; })()"
                    % (json.dumps(option.strip().lower()), json.dumps(json.dumps(f.field_key)), json.dumps(json.dumps(f.field_key)))
                ))
            tid = _TESTIDS.get(f.field_key)
            if tid:
                await session.fill('[data-testid=%s]' % json.dumps(tid), f.value)
                return True
            if f.field_key.startswith("rippling_"):
                if not await session.eval_js(_STAMP_COMBOBOX_JS % (json.dumps(f.label.strip().lower()), json.dumps(f.field_key))):
                    return False
                sel = '[data-apply-field=%s]' % json.dumps(f.field_key)
                await session.click(sel)
                await session.fill(sel, f.value)
                await asyncio.sleep(_COMBOBOX_SETTLE_SECONDS)
                options = await session.eval_js("() => [...document.querySelectorAll('[role=option]')].map(o => o.innerText.trim())") or []
                if options:
                    choice = choose_combobox_option(f.value, [str(o) for o in options])
                    if choice is None:
                        return False
                    await session.eval_js(
                        "(() => { const o = [...document.querySelectorAll('[role=option]')].find(o => o.innerText.trim() === %s);"
                        " if (o) o.click(); return !!o; })()" % json.dumps(choice))
                return True
            return False
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
            w = (coerce_option(f.value, f.options or ["Yes", "No"]) or "").strip().lower()
            return bool(w) and (got_n == w or w in got_n)
        wanted = (f.value or "").strip().lower()
        digits = "".join(c for c in wanted if c.isdigit())
        if len(digits) >= 7 and re.fullmatch(r"[\d+\-() .]+", wanted):
            gd = "".join(c for c in got_n if c.isdigit())
            return bool(gd) and (digits.endswith(gd) or gd.endswith(digits))
        if wanted[:24] in got_n:
            return True
        toks = [t for t in re.split(r"[^\w]+", wanted) if len(t) >= 3]
        return bool(toks) and all(t in got_n for t in toks)

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
            if "/success" in url or "thank" in url or url.endswith("/submitted"):
                return f"redirected to {url}"
            await asyncio.sleep(1)
        return None
