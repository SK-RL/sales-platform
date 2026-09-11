"""Gem application submitter.

F391. Verified on jobs.gem.com/{board}/{extId} (gem, modular): the
application form is on the posting page. Controls carry no ``name`` or
``id``, so text inputs and textareas are addressed by the question text
in their enclosing block (the form's own fixed labels "First name",
"Last name", "Email", "LinkedIn URL", "Phone number", "Location" and
custom questions' text); choice options are radios/checkboxes whose
``id`` is the option's ``extId`` from the GraphQL schema, clicked via
their label. The résumé is the hidden file input in the upload block.
Submit is "Apply without saving" (the other button also creates a Gem
profile, which we never do). hCaptcha here is ``checkbox-invisible``
(runs on submit); a visible challenge would stop the run via
``detect_human_wall``.
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

_FIXED_LABELS: dict[str, str] = {
    "first_name": "First name",
    "last_name": "Last name",
    "email": "Email",
    "linkedin": "LinkedIn URL",
    "phone": "Phone number",
    "location": "Location",
}
_FORM_READY_SELECTOR = "input[type=text]"
_RESUME_SELECTOR = "input[type=file]"
_SUBMIT_SELECTOR = 'button:has-text("Apply without saving"), button:has-text("Submit application"), button:has-text("Apply")'
_CONFIRMATION_WAIT_SECONDS = 25

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "application submitted",
    "application has been submitted",
    "we have received your application",
    "your application has been received",
)

# Shared helper: find the text/textarea control for a question label.
_CONTROL_BY_LABEL_JS = r"""
const norm = t => (t || '').replace(/\s+/g, ' ').trim().replace(/\s*\*+\s*$/, '').toLowerCase();
const controlFor = (want) => {
  const ctls = [...document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]), textarea')]
    .filter(e => e.name !== 'g-recaptcha-response' && e.name !== 'h-captcha-response' && e.offsetParent);
  const kind = 'input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]), textarea';
  for (const e of ctls) {
    let n = e;
    for (let i = 0; i < 6 && n; i++) {
      n = n.parentElement; if (!n) break;
      // Only a block that wraps this one control may carry its label;
      // a shared ancestor (the whole form) mentions every label.
      if (n.querySelectorAll(kind).length > 1 || n.tagName === 'FORM') break;
      const l = [...n.querySelectorAll('label, legend, p, span, div')].find(x => x.children.length <= 1 && norm(x.innerText) === want);
      if (l) return e;
    }
  }
  return null;
};
"""

_READBACK_JS = r"""
(() => {
  """ + _CONTROL_BY_LABEL_JS + r"""
  const labels = %s; const out = {};
  for (const [key, label] of Object.entries(labels)) { const e = controlFor(norm(label)); out[key] = e ? (e.value || '') : null; }
  const optionIds = %s;
  for (const [key, ids] of Object.entries(optionIds)) {
    const picked = [];
    for (const id of ids) { const r = document.getElementById(id); if (r && r.checked) { const l = document.querySelector('label[for="' + CSS.escape(id) + '"]'); picked.push((l ? l.innerText : r.value || '').replace(/\s+/g, ' ').trim()); } }
    out[key] = picked.join(' | ');
  }
  return out;
})()
"""

_FILL_BY_LABEL_JS = r"""
(() => {
  """ + _CONTROL_BY_LABEL_JS + r"""
  const e = controlFor(norm(%s)); if (!e) return false;
  e.focus(); return true;
})()
"""

_CLICK_OPTION_JS = r"""
(() => {
  const r = document.getElementById(%s); if (!r) return false;
  const l = document.querySelector('label[for="' + CSS.escape(r.id) + '"]');
  if (!r.checked) (l || r).click();
  return r.checked;
})()
"""


class GemSubmitter(BaseSubmitter):
    platform = "gem"

    async def submit(self, *, job_url: str, fields: list[SubmitField], resume_path: str | None = None,
                     cover_letter_text: str | None = None, dry_run: bool = False) -> SubmitOutcome:
        unplaceable: list[str] = []
        issues: list[str] = []
        try:
            self._load_schema(job_url)
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
                actual = await self._readback(session, fields)
                for f in fields:
                    if f.field_key in unplaceable or f.field_type == "file":
                        continue
                    if not self._value_present(f, str(actual.get(f.field_key) or "")):
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
                                         error="submitted the form but saw no confirmation from Gem")
                return SubmitOutcome(status="submitted", confirmation_text=confirmation, detected_issues=issues,
                                     unplaceable_fields=unplaceable)
        except BlockedBySite as exc:
            return SubmitOutcome(status="blocked", error=str(exc))
        except BrowserError as exc:
            return SubmitOutcome(status="failed", error=f"browser error: {exc}", unplaceable_fields=unplaceable,
                                 detected_issues=issues)

    @staticmethod
    def _label(f: SubmitField) -> str:
        return _FIXED_LABELS.get(f.field_key, f.label or "")

    _schema: dict[str, dict[str, str]]

    def __init__(self) -> None:
        self._schema = {}

    def _load_schema(self, job_url: str) -> None:
        """question extId -> {option text: option extId}, from the same
        public GraphQL schema the extractor read (one POST)."""
        from app.fetchers.gem import GemFetcher

        m = re.match(r"^https?://jobs\.gem\.com/([^/?#]+)/([^/?#]+)", job_url or "")
        if not m:
            return
        try:
            data = GemFetcher().posting(m.group(1), m.group(2))
        except Exception:
            logger.info("gem: could not load the form schema for %s", job_url, exc_info=True)
            return
        form = data.get("oatsJobPostFieldsAndQuestions") or {}
        for q in list(form.get("questions") or []) + list((form.get("demographicSurvey") or {}).get("questions") or []):
            ext = str(q.get("extId") or "")
            if ext:
                self._schema[ext] = {str(o.get("value") or ""): str(o.get("extId") or "") for o in (q.get("options") or []) if isinstance(o, dict)}

    def _option_ids(self, f: SubmitField) -> dict[str, str]:
        """option text -> the DOM id of its radio/checkbox (the option extId)."""
        return {k: v for k, v in (self._schema.get(f.field_key) or {}).items() if k and v}

    async def _place(self, session: BrowserSession, f: SubmitField, resume_uploaded: bool) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded
        try:
            if f.field_type in ("select", "multi_select", "boolean"):
                ids = self._option_ids(f)
                if not ids:
                    return False
                wanted = [v.strip() for v in re.split(r"\s*[;|]\s*", f.value or "") if v.strip()] if f.field_type == "multi_select" else [f.value]
                chosen = [c for c in (coerce_option(v, list(ids)) for v in wanted) if c]
                if not chosen:
                    return False
                ok = True
                for c in chosen:
                    ok = bool(await session.eval_js(_CLICK_OPTION_JS % json.dumps(ids[c]))) and ok
                return ok
            if not await session.eval_js(_FILL_BY_LABEL_JS % json.dumps(self._label(f))):
                return False
            # The focused control is the one we found; type into it.
            await session.eval_js("document.activeElement && document.activeElement.setAttribute('data-apply-target', '1')")
            await session.fill("[data-apply-target='1']", f.value)
            await session.eval_js("document.querySelectorAll('[data-apply-target]').forEach(e => e.removeAttribute('data-apply-target'))")
            return True
        except BrowserError:
            return False

    async def _readback(self, session: BrowserSession, fields: list[SubmitField]) -> dict:
        labels = {f.field_key: self._label(f) for f in fields if f.field_type in ("text", "textarea")}
        option_ids = {f.field_key: list(self._option_ids(f).values()) for f in fields if f.field_type in ("select", "multi_select", "boolean")}
        try:
            return await session.eval_js(_READBACK_JS % (json.dumps(labels), json.dumps(option_ids))) or {}
        except BrowserError:
            return {}

    @staticmethod
    def _value_present(f: SubmitField, got: str) -> bool:
        got_n = (got or "").strip().lower()
        if not got_n:
            return False
        if f.field_type in ("select", "multi_select", "boolean"):
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
            if "thank" in url or "success" in url or "confirmation" in url:
                return f"redirected to {url}"
            await asyncio.sleep(1)
        return None
