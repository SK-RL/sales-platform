"""Recruitee application submitter.

Verified against a live posting (jobs.channable.com — Recruitee
white-label, offer 2723126, 9 custom questions). Recruitee is almost the
mirror image of Greenhouse, so nothing from that adapter transfers:

* **Fields are keyed by ``name``, not ``id``.** Greenhouse is the
  opposite. Recruitee's ``id`` carries a positional suffix
  (``input-candidate.name-3``) that shifts when the form changes, so
  ``name`` is the only stable handle.
* **The apply form's question ids are NOT the public API's question
  ids.** ``/api/offers/{id}`` returns ``open_questions[].id = 4336435``
  while the form uses ``candidate.openQuestionAnswers.7465115.*``. The
  two id spaces share no bridge — the number appears nowhere in the API
  payload, there is no XHR to intercept (the form is server-rendered),
  and the hidden ``…openQuestionId`` field just repeats the DOM id.
  So we resolve fields by **question text**, matching the label
  rendered in each question's container against the ``body`` our
  extractor read from the same API. Unmatched means unplaceable, which
  makes a mismatch fail safe: the gate blocks rather than answering the
  wrong question.
* **Answer field name depends on the question kind**: booleans write to
  ``.flag`` (radio, value ``"true"``/``"false"``), everything else to
  ``.content``. Choice questions are radios whose ``value`` is the
  option's own text, so ``coerce_option`` output drops straight in.
* **No native ``<select>``** anywhere, same as Greenhouse — but here the
  controls are plain radios rather than a JS combobox, so they can be
  clicked directly.

Submit-button hazard
--------------------
These boards ship a cookie-consent dialog containing a dozen
``button[type=submit]`` elements ("Allow all", "Necessary", …). A
generic ``button[type=submit]`` selector — which is what the Greenhouse
adapter falls back to — clicks a *cookie banner button* here. The real
control is a ``Send`` button inside the application ``<form>``, so the
selector is scoped to the form and matched on its text.
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

# Our extractor's field_key -> the form's `name` attribute.
_FIXED_NAMES: dict[str, str] = {
    "name": "candidate.name",
    "email": "candidate.email",
    "phone": "candidate.phone",
    "resume": "candidate.cv",
    "cover_letter": "candidate.cover_letter",
}

_FORM_READY_SELECTOR = 'input[name="candidate.name"]'
_HYDRATION_SETTLE_SECONDS = 2.0

# Scoped to the application form — see the module note on the cookie
# dialog's submit buttons.
_SUBMIT_SELECTOR = 'form button[type=submit]'

_CONFIRMATION_MARKERS: tuple[str, ...] = (
    "thank you for applying",
    "thanks for applying",
    "your application has been",
    "application received",
    "we have received your application",
)

# Enumerates every custom question with its rendered label, the DOM id
# Recruitee assigned it, and the inputs available. Returned to Python so
# field resolution (label -> dom id) happens in one round trip.
_RESOLVE_JS = """
(() => {
  const norm = t => (t||'').replace(/\\s+/g,' ').replace(/\\*/g,'').trim().toLowerCase();
  return [...document.querySelectorAll('input[name$=".openQuestionId"]')].map(h => {
    const domId = h.name.split('.')[2];
    let n = h.parentElement, box = null, hops = 0;
    while (n && hops < 8) {
      if (n.querySelectorAll('input[name$=".openQuestionId"]').length === 1) { box = n; }
      else break;
      n = n.parentElement; hops++;
    }
    const sel = `[name^="candidate.openQuestionAnswers.${domId}."]`;
    const inputs = box ? [...box.querySelectorAll(sel)].filter(e => e.type !== 'hidden') : [];
    return {
      domId,
      label: norm(box ? box.innerText : ''),
      suffix: inputs.length ? inputs[0].name.split('.').pop() : '',
      type: inputs.length ? inputs[0].type : '',
      // Radio `id` is what the visible <label for=...> points at. The
      // input itself is 1x1px behind a styled fake and clicking it
      // does nothing — React reverts the state.
      radios: inputs.filter(i => i.type === 'radio').map(i => ({value: i.value, id: i.id})),
      values: inputs.filter(i => i.type === 'radio').map(i => i.value),
    };
  });
})()
"""

_READBACK_JS = """
(() => {
  const out = {};
  document.querySelectorAll('input,textarea').forEach(e => {
    if (!e.name) return;
    if (e.type === 'radio' || e.type === 'checkbox') {
      if (e.checked) out[e.name] = e.value;
      else if (!(e.name in out)) out[e.name] = '';
    } else {
      out[e.name] = e.value || '';
    }
  });
  return out;
})()
"""


def _norm(text: str) -> str:
    return " ".join((text or "").replace("*", " ").split()).strip().lower()


class RecruiteeSubmitter(BaseSubmitter):
    platform = "recruitee"

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
                    job_url,
                    wait_until="domcontentloaded",
                    wait_for_selector=_FORM_READY_SELECTOR,
                )
                await asyncio.sleep(_HYDRATION_SETTLE_SECONDS)

                html = await session.html()
                wall = detect_human_wall(html)
                if wall:
                    raise BlockedBySite(f"page requires a human: {wall}")

                resolved = await self._resolve(session)

                resume_uploaded = False
                if resume_path:
                    try:
                        await session.upload(
                            f'input[name="{_FIXED_NAMES["resume"]}"]', resume_path
                        )
                        resume_uploaded = True
                    except BrowserError:
                        pass

                for f in fields:
                    if not await self._place(session, f, resolved, resume_uploaded):
                        unplaceable.append(f.field_key)

                # Never trust fill(); confirm against the DOM.
                actual = await self._readback(session)
                for f in fields:
                    if f.field_key in unplaceable or f.field_type == "file":
                        continue
                    dom_name = self._dom_name(f, resolved)
                    if not dom_name:
                        continue
                    got = str(actual.get(dom_name, ""))
                    if not self._value_present(f, got):
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
                        error=(
                            "required fields could not be placed: "
                            + ", ".join(required_missing)
                        ),
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
                        error="submitted the form but saw no confirmation from Recruitee",
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

    async def _resolve(self, session: BrowserSession) -> dict:
        """label -> {domId, suffix, type, values} for every question."""
        try:
            rows = await session.eval_js(_RESOLVE_JS) or []
        except BrowserError:
            return {}
        return {r["label"]: r for r in rows if r.get("label")}

    def _dom_name(self, f: SubmitField, resolved: dict) -> str:
        row = self._row_for(f, resolved)
        if f.field_key in _FIXED_NAMES:
            return _FIXED_NAMES[f.field_key]
        if not row:
            return ""
        return f"candidate.openQuestionAnswers.{row['domId']}.{row['suffix']}"

    @staticmethod
    def _row_for(f: SubmitField, resolved: dict) -> dict | None:
        """Find the rendered question matching this field's label.

        Exact match first, then *unique* containment. Containment is
        needed because a question's container text includes its own
        controls: a boolean renders as
        ``"Do you live in the Netherlands? * Yes No"`` while the API
        body is just ``"Do you live in the Netherlands?"``. That
        mismatch is why every radio question was unplaceable on the
        first live run.

        Ambiguity fails safe. If two questions contain the body we
        return None, the field is reported unplaceable, and the gate
        blocks — far better than answering the wrong question, which is
        the whole failure mode this adapter exists to avoid.
        """
        want = _norm(f.label)
        if not want:
            return None
        if want in resolved:
            return resolved[want]
        hits = [row for label, row in resolved.items() if want in label]
        return hits[0] if len(hits) == 1 else None

    async def _place(
        self,
        session: BrowserSession,
        f: SubmitField,
        resolved: dict,
        resume_uploaded: bool,
    ) -> bool:
        if f.field_type == "file":
            # Includes Recruitee's `video` kind, which renders a recorder
            # widget with no fillable input. Reported placed only for the
            # resume we actually uploaded.
            return f.field_key == "resume" and resume_uploaded

        dom_name = self._dom_name(f, resolved)
        if not dom_name:
            # No question matched this label — fail safe rather than
            # guess which box it belongs in.
            return False

        row = self._row_for(f, resolved)
        try:
            if f.field_type == "boolean":
                wanted = coerce_option(f.value, ["Yes", "No"])
                if wanted is None:
                    return False
                return await self._pick_radio(
                    session, row, "true" if wanted.lower() == "yes" else "false"
                )

            if f.field_type in ("select", "multi_select"):
                option = coerce_option(f.value, f.options)
                if option is None:
                    return False
                # Radio `value` is the option's own text, so the coerced
                # option drops straight in — but only if it really is one
                # of the rendered choices. A value we invented would
                # silently click nothing.
                return await self._pick_radio(session, row, option)

            await session.fill(f'[name="{dom_name}"]', f.value)
            return True
        except BrowserError:
            return False

    @staticmethod
    async def _pick_radio(session: BrowserSession, row: dict | None, value: str) -> bool:
        """Select a radio by clicking its visible label.

        The real ``<input type=radio>`` is 1x1px and absolutely
        positioned behind a styled fake. Playwright's ``check()`` on it
        fails with "Clicking the checkbox did not change its state" —
        the click lands but React reverts it. Each radio does have a
        proper ``<label for=...>`` (79x42px on the live form), so we
        click that instead, addressing it by the radio's id.
        """
        if not row:
            return False
        match = next(
            (r for r in (row.get("radios") or []) if str(r.get("value")) == value),
            None,
        )
        if not match or not match.get("id"):
            return False
        # Playwright's click() times out here: something overlays the
        # control (elementFromPoint at the label's centre returns a
        # different DIV), so the actionability check never passes. A JS
        # click on the label bypasses pointer-event interception while
        # still firing the events React listens for — verified live,
        # the radio goes from unchecked to checked. The readback pass in
        # submit() is what proves it actually stuck, so this can't
        # silently no-op the way `check()` did.
        script = (
            "(() => { const l = document.querySelector('label[for=' + %s + ']');"
            " if (!l) return false; l.click(); return true; })()"
            % json.dumps(json.dumps(match["id"]))
        )
        try:
            return bool(await session.eval_js(script))
        except BrowserError:
            return False

    async def _readback(self, session: BrowserSession) -> dict:
        try:
            return await session.eval_js(_READBACK_JS) or {}
        except BrowserError:
            return {}

    @staticmethod
    def _esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _value_present(f: SubmitField, got: str) -> bool:
        got_n = (got or "").strip().lower()
        if not got_n:
            return False
        if f.field_type == "boolean":
            wanted = coerce_option(f.value, ["Yes", "No"])
            return got_n == ("true" if (wanted or "").lower() == "yes" else "false")
        if f.field_type in ("select", "multi_select"):
            return (f.value or "").strip().lower() in got_n or got_n in (
                f.value or ""
            ).strip().lower()
        wanted = (f.value or "").strip().lower()
        # Phone widgets reformat as you type (Recruitee uses intl-tel,
        # which strips the country code into a separate selector), so a
        # literal comparison false-alarms. Compare digits when the
        # intended value is essentially a number.
        digits = "".join(c for c in wanted if c.isdigit())
        if digits and len(digits) >= len(wanted) - 3:
            got_digits = "".join(c for c in got_n if c.isdigit())
            return bool(got_digits) and (
                digits.endswith(got_digits) or got_digits.endswith(digits)
            )
        return bool(wanted) and wanted[:24] in got_n

    @staticmethod
    async def _await_confirmation(session: BrowserSession) -> str | None:
        try:
            html = (await session.html()).lower()
        except BrowserError:
            return None
        for marker in _CONFIRMATION_MARKERS:
            if marker in html:
                return marker
        try:
            url = (await session.url()).lower()
        except BrowserError:
            return None
        return f"redirected to {url}" if "/c/thanks" in url else None
