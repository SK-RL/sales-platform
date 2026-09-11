"""Ashby application submitter.

F368. Built against the rendered application page of a live Ramp
posting (jobs.ashbyhq.com/ramp/34413f8d-…/application), the same page
``fetchers/questions.ashby_form_rows`` extracts from, so every
``field_key`` the user approved is addressed here by the same token.

What inspection settled:

* **Ashby's reCAPTCHA is v3, not a checkbox.** ``api.js?render=<key>``,
  an anchor iframe with ``size=invisible``, a badge, no widget and no
  "I'm not a robot" anywhere. F366 classed it as a wall on the bare
  ``api2/anchor`` marker and made Ashby extraction-only; that marker
  now discriminates (``base._interactive_recaptcha``). Nothing here
  touches the captcha — the token is minted by Ashby's own script on
  submit, exactly as on Greenhouse.
* **Every field lives in a ``[class*="_fieldEntry"]`` wrapper** whose
  first ``<label>`` is the question. System fields have stable ids
  (``_systemfield_name``, ``_systemfield_email``,
  ``_systemfield_resume``); custom questions are a UUID that is both id
  and name; the payroll-location field has neither and is only
  reachable through its wrapper's label — which is also how the
  extractor keys it (``ashby_<slug>``).
* **Yes/No questions are two ``<button type=submit>`` elements** with
  ``aria-pressed`` and a hidden checkbox behind them. Clicking "Yes"
  checks the box; clicking "No" leaves it unchecked and presses the
  button. Only the buttons tell you what was chosen, so readback reads
  ``aria-pressed``, not ``.checked``.
* **The location field is a combobox** (``input[role=combobox]``) that
  offers ``[role=option]`` results as you type. We pick an option only
  when every word of the saved answer appears in it; a result that
  merely comes first is not the user's answer.
* **Upload and Yes/No buttons are also ``type=submit``**, so the submit
  control cannot be ``button[type=submit]``. It is addressed by its text,
  "Submit Application".
* **Confirmation copy** is ``"Your application was successfully
  submitted."`` (pulled from Ashby's own bundle, not guessed).

As with every adapter: fills are verified by DOM readback, an
unplaceable *required* field aborts before the click, and only a
confirmation from Ashby flips the outcome to ``submitted``.
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

_APPLY_SUFFIX = "/application"
_FORM_READY_SELECTOR = "#_systemfield_name"
_RESUME_SELECTOR = "#_systemfield_resume"
# Text-addressed on purpose: Upload File and Yes/No are `type=submit` too.
_SUBMIT_SELECTOR = 'button[type=submit]:has-text("Submit Application")'
_HYDRATION_SETTLE_SECONDS = 2.0
_COMBOBOX_SETTLE_SECONDS = 1.5
_CONFIRMATION_WAIT_SECONDS = 20

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "your application was successfully submitted",
    "application submitted",
    "successfully submitted",
    "thank you for applying",
    "thanks for applying",
)

# One row per field wrapper, in DOM order. Python does the matching so
# the label→key rule is the extractor's own `_normalise_field_key`.
_ENTRIES_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('[class*="_fieldEntry"]')].map((w, idx) => {
    const ctrls = [...w.querySelectorAll('input,textarea')];
    return {
      idx,
      label: norm(w.querySelector('label, [class*="label"], [class*="Label"]')?.innerText || ''),
      ids: ctrls.map(c => c.id).filter(Boolean),
      names: ctrls.map(c => c.name).filter(Boolean),
      types: ctrls.map(c => (c.tagName === 'TEXTAREA' ? 'textarea' : c.type)),
      ctrls: ctrls.map((c, ci) => ({ci, id: c.id || '', name: c.name || '', type: (c.tagName === 'TEXTAREA' ? 'textarea' : c.type), combobox: c.getAttribute('role') === 'combobox'})),
      combobox: !!w.querySelector('input[role=combobox]'),
      yesno: [...w.querySelectorAll('button')].some(b => /^(yes|no)$/i.test(norm(b.innerText))),
    };
  });
})()
"""

# Tag a wrapper (choice groups) or a single control (text-like fields)
# so placing and readback address exactly what was resolved. Two
# stamps, because one wrapper can hold two questions: on the live form
# the Phone entry contains both the tel input and the SMS-consent
# radios, and a wrapper-only stamp let the second overwrite the first —
# phone then read back as the consent label and was demoted.
_STAMP_GROUP_JS = "(() => { const w = document.querySelectorAll('[class*=\"_fieldEntry\"]')[%d]; if (!w) return false; w.setAttribute('data-apply-group', %s); return true; })()"
_STAMP_CONTROL_JS = "(() => { const w = document.querySelectorAll('[class*=\"_fieldEntry\"]')[%d]; const c = w && w.querySelectorAll('input,textarea')[%d]; if (!c) return false; c.setAttribute('data-apply-field', %s); return true; })()"

_TEXT_FIELD = "[data-apply-field=%s]"

# Every stamped field/group's current value, as a person would read it.
_READBACK_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const c of document.querySelectorAll('[data-apply-field]')) {
    out[c.getAttribute('data-apply-field')] = c.value || '';
  }
  for (const w of document.querySelectorAll('[data-apply-group]')) {
    const key = w.getAttribute('data-apply-group');
    const pressed = [...w.querySelectorAll('button[aria-pressed="true"]')].map(b => norm(b.innerText));
    if (pressed.length) { out[key] = pressed.join(' | '); continue; }
    const radio = [...w.querySelectorAll('input[type=radio]')].find(r => r.checked);
    if (radio) {
      const lab = (radio.id && w.querySelector(`label[for="${CSS.escape(radio.id)}"]`)) || radio.closest('label');
      out[key] = norm(lab ? lab.innerText : radio.value); continue;
    }
    const ctrl = w.querySelector('input[role=combobox], input:not([type=file]):not([type=radio]):not([type=checkbox]), textarea');
    out[key] = ctrl ? (ctrl.value || '') : '';
  }
  return out;
})()
"""


def _slug(text: str) -> str:
    """Same rule as ``fetchers.questions._normalise_field_key``."""
    key = (text or "").lower().strip()
    key = re.sub(r"[^\w\s]", "", key)
    return re.sub(r"\s+", "_", key)[:255]


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w]+", (text or "").lower()) if len(t) >= 3]


def resolve_entry(f: SubmitField, entries: list[dict]) -> int | None:
    """Index of the wrapper holding this field, or None. Pure.

    Ambiguity fails safe: two wrappers with the same question text is
    a form we do not understand well enough to fill.
    """
    key = f.field_key
    system = {"name": "_systemfield_name", "email": "_systemfield_email", "resume": "_systemfield_resume"}
    if key in system:
        hits = [e["idx"] for e in entries if system[key] in e["ids"]]
        return hits[0] if hits else None
    if key == "phone":
        hits = [e["idx"] for e in entries if "tel" in e["types"]]
        return hits[0] if len(hits) == 1 else None
    if key == "cover_letter_file":
        hits = [e["idx"] for e in entries if "file" in e["types"] and e["label"].lower().startswith("cover")]
        return hits[0] if len(hits) == 1 else None
    hits = [e["idx"] for e in entries if key in e["ids"] or key in e["names"]]
    if hits:
        return hits[0]
    wanted = key[len("ashby_"):] if key.startswith("ashby_") else _slug(f.label)
    if not wanted:
        return None
    hits = [e["idx"] for e in entries if _slug(e["label"]) == wanted]
    return hits[0] if len(hits) == 1 else None


_TEXT_TYPES = {"text", "email", "tel", "url", "number", "textarea", "search"}


def resolve_control(f: SubmitField, entry: dict) -> int | None:
    """Index of the text-like control inside ``entry`` for this field.

    None for choice groups (handled at wrapper level) and when nothing
    text-like is there.
    """
    key = f.field_key
    ctrls = entry.get("ctrls") or []
    system = {"name": "_systemfield_name", "email": "_systemfield_email"}
    if key in system:
        return next((c["ci"] for c in ctrls if c["id"] == system[key]), None)
    if key == "phone":
        return next((c["ci"] for c in ctrls if c["type"] == "tel"), None)
    hit = next((c["ci"] for c in ctrls if (c["id"] == key or c["name"] == key) and c["type"] in _TEXT_TYPES), None)
    if hit is not None:
        return hit
    return next((c["ci"] for c in ctrls if c["type"] in _TEXT_TYPES and not c["combobox"]), None)


def choose_combobox_option(value: str, options: list[str]) -> str | None:
    """The option a person would pick for ``value``, or None.

    Every word (3+ chars) of the answer must appear in the option; among
    those the shortest wins, since "New York, United States" is a closer
    reading of "New York" than "New York Mills, Minnesota". Nothing
    matches → nothing chosen (F346: unplaceable beats a guess).
    """
    want = _tokens(value)
    if not want:
        return None
    exact = [o for o in options if o.strip().lower() == value.strip().lower()]
    if exact:
        return exact[0]
    good = [o for o in options if all(t in _tokens(o) for t in want)]
    return min(good, key=len) if good else None


class AshbySubmitter(BaseSubmitter):
    platform = "ashby"

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
                    # Ashby takes the cover letter as a file, which we
                    # don't synthesise; note it rather than fail.
                    issues.append("cover_letter_field_absent")

                entries = await self._entries(session)
                for f in fields:
                    if not await self._place(session, f, entries, resume_uploaded):
                        unplaceable.append(f.field_key)

                await asyncio.sleep(0.5)
                actual = await self._readback(session)
                for f in fields:
                    if f.field_key in unplaceable or f.field_type == "file":
                        continue
                    if not self._value_present(f, str(actual.get(f.field_key, ""))):
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
                        error="submitted the form but saw no confirmation from Ashby",
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
        """The scanner stores the posting page; the form is /application."""
        u = (job_url or "").split("?", 1)[0].rstrip("/")
        return u if u.endswith(_APPLY_SUFFIX) else u + _APPLY_SUFFIX

    async def _entries(self, session: BrowserSession) -> list[dict]:
        try:
            return await session.eval_js(_ENTRIES_JS) or []
        except BrowserError:
            return []

    async def _place(
        self, session: BrowserSession, f: SubmitField, entries: list[dict], resume_uploaded: bool
    ) -> bool:
        if f.field_type == "file":
            return f.field_key == "resume" and resume_uploaded

        idx = resolve_entry(f, entries)
        if idx is None:
            return False
        entry = entries[idx]
        key_js = json.dumps(f.field_key)
        try:
            is_choice = (
                f.field_type in ("boolean", "select", "multi_select")
                or bool(entry.get("combobox"))
            )
            if is_choice:
                if not await session.eval_js(_STAMP_GROUP_JS % (idx, key_js)):
                    return False
            else:
                ci = resolve_control(f, entry)
                if ci is None or not await session.eval_js(_STAMP_CONTROL_JS % (idx, ci, key_js)):
                    return False

            if f.field_type == "boolean" or (entry.get("yesno") and f.field_type in ("select", "boolean")):
                option = coerce_option(f.value, ["Yes", "No"])
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const w = document.querySelector('[data-apply-group=' + %s + ']');"
                    " const b = [...w.querySelectorAll('button')].find(b => b.innerText.trim().toLowerCase() === %s);"
                    " if (!b) return false; b.click(); return true; })()"
                    % (json.dumps(key_js), json.dumps(option.lower()))
                ))

            if f.field_type in ("select", "multi_select"):
                option = coerce_option(f.value, f.options)
                if option is None:
                    return False
                return bool(await session.eval_js(
                    "(() => { const norm = t => (t||'').replace(/\\s+/g,' ').trim().toLowerCase();"
                    " const w = document.querySelector('[data-apply-group=' + %s + ']');"
                    " for (const r of w.querySelectorAll('input[type=radio]')) {"
                    "   const lab = (r.id && w.querySelector('label[for=\"' + CSS.escape(r.id) + '\"]')) || r.closest('label');"
                    "   if (norm(lab ? lab.innerText : r.value) === %s) { (lab || r).click(); return true; } }"
                    " return false; })()"
                    % (json.dumps(key_js), json.dumps(option.strip().lower()))
                ))

            if entry.get("combobox"):
                return await self._place_combobox(session, key_js, f.value)

            await session.fill(_TEXT_FIELD % key_js, f.value)
            return True
        except BrowserError:
            return False

    async def _place_combobox(self, session: BrowserSession, key_js: str, value: str) -> bool:
        sel = "[data-apply-group=%s] input[role=combobox]" % key_js
        await session.click(sel)
        await session.fill(sel, value)
        await asyncio.sleep(_COMBOBOX_SETTLE_SECONDS)
        options = await session.eval_js(
            "(() => { const w = document.querySelector('[data-apply-group=' + %s + ']');"
            " const scope = w.querySelector('[role=option]') ? w : document;"
            " return [...scope.querySelectorAll('[role=option]')].map(o => o.innerText.trim()); })()"
            % json.dumps(key_js)
        ) or []
        choice = choose_combobox_option(value, [str(o) for o in options])
        if choice is None:
            return False
        return bool(await session.eval_js(
            "(() => { const w = document.querySelector('[data-apply-group=' + %s + ']');"
            " const scope = w.querySelector('[role=option]') ? w : document;"
            " const o = [...scope.querySelectorAll('[role=option]')].find(o => o.innerText.trim() === %s);"
            " if (!o) return false; o.click(); return true; })()"
            % (json.dumps(key_js), json.dumps(choice))
        ))

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
            return bool(wanted) and got_n == wanted
        if f.field_type in ("select", "multi_select"):
            w = (coerce_option(f.value, f.options) or f.value or "").strip().lower()
            return bool(w) and (w in got_n or got_n in w)
        wanted = (f.value or "").strip().lower()
        # Phone-like: compare digits only, since the field may mask
        # "+1 415 555 0100" as "(415) 555-0100" or strip the spaces.
        digits = "".join(c for c in wanted if c.isdigit())
        if len(digits) >= 7 and re.fullmatch(r"[\d+\-() .]+", wanted):
            gd = "".join(c for c in got_n if c.isdigit())
            return bool(gd) and (digits.endswith(gd) or gd.endswith(digits))
        if wanted[:24] in got_n:
            return True
        # A combobox commits the option's text, not what was typed.
        want = _tokens(wanted)
        return bool(want) and all(t in _tokens(got_n) for t in want)

    @staticmethod
    async def _await_confirmation(session: BrowserSession) -> str | None:
        """Poll for Ashby's success copy — the submit is an XHR and the
        card renders after it returns, so a single read right after the
        click would miss a real submission."""
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
