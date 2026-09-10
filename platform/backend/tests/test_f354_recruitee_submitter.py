"""F354 — Recruitee submitter, built against a live form.

Verified on jobs.channable.com (Recruitee white-label). Recruitee is
almost the mirror image of Greenhouse, so none of that adapter's
assumptions carried over. Every fact pinned here cost a failed live dry
run to discover:

1. **Keyed by ``name``, not ``id``** — the opposite of Greenhouse, whose
   ids are stable and names absent. Recruitee's ids carry a positional
   suffix that shifts when the form changes.
2. **API question ids ≠ form question ids.** ``/api/offers/{id}`` returns
   ``open_questions[].id = 4336435``; the form uses
   ``candidate.openQuestionAnswers.7465115.*``. No bridge exists — the
   number appears nowhere in the API payload, the form is
   server-rendered (no XHR to intercept), and the hidden
   ``…openQuestionId`` input just repeats the DOM id. So fields resolve
   by question *text*.
3. **Container text includes the controls.** A boolean renders as
   ``"Do you live in the Netherlands? * Yes No"`` while the API body is
   just the question, so matching must be containment, not equality.
   First live run: every radio question unplaceable.
4. **``check()`` does not work on these radios.** The real input is
   1x1px behind a styled fake; Playwright reports "Clicking the checkbox
   did not change its state" — React reverts it.
5. **Nor does clicking the label with Playwright** — something overlays
   the control (``elementFromPoint`` at the label's centre returns a
   different DIV), so actionability never passes and click() times out.
   A JS ``label.click()`` works: verified unchecked -> checked.

Final live result on a video-free posting: 10 fields, status=submitted,
error=None, everything confirmed by readback.
"""

import pytest

from app.services.submitters import (
    auto_submittable_platforms,
    get_submitter,
    submit_platforms,
)
from app.services.submitters.base import SubmitField
from app.services.submitters.recruitee import (
    _FIXED_NAMES,
    _READBACK_JS,
    _RESOLVE_JS,
    _SUBMIT_SELECTOR,
    RecruiteeSubmitter,
    _norm,
)


def _f(key, label="", ftype="text", value="x", options=None):
    return SubmitField(
        field_key=key, label=label or key, field_type=ftype, value=value,
        options=options or [],
    )


# As returned by _RESOLVE_JS against the live form.
RESOLVED = {
    "do you live in the netherlands?  yes no": {
        "domId": "7465118", "suffix": "flag", "type": "radio",
        "radios": [
            {"value": "true", "id": "input-candidate.openQuestionAnswers.7465118.flag-14-0"},
            {"value": "false", "id": "input-candidate.openQuestionAnswers.7465118.flag-14-1"},
        ],
        "values": ["true", "false"],
    },
    "which city/town do you live in?": {
        "domId": "7465192", "suffix": "content", "type": "text",
        "radios": [], "values": [],
    },
    "do you have eu citizenship or a valid work permit for the netherlands?  eu citizenship valid work permit i need sponsorship from channable": {
        "domId": "7465117", "suffix": "content", "type": "radio",
        "radios": [
            {"value": "EU citizenship", "id": "input-candidate.openQuestionAnswers.7465117.content-13-0"},
            {"value": "Valid work permit", "id": "input-candidate.openQuestionAnswers.7465117.content-13-1"},
        ],
        "values": ["EU citizenship", "Valid work permit"],
    },
}


class TestRegistry:
    def test_recruitee_is_registered(self):
        assert isinstance(get_submitter("recruitee"), RecruiteeSubmitter)

    def test_it_is_auto_submittable(self):
        """Extraction (F348) plus submission (this) — both halves."""
        assert "recruitee" in submit_platforms()
        assert "recruitee" in auto_submittable_platforms()


class TestSubmitSelectorHazard:
    def test_submit_is_scoped_to_the_form(self):
        """These boards ship a cookie dialog containing a dozen
        button[type=submit] elements ("Allow all", "Necessary"…). An
        unscoped selector — Greenhouse's fallback — clicks a cookie
        button here."""
        assert _SUBMIT_SELECTOR.startswith("form ")


class TestLabelResolution:
    def test_exact_match(self):
        f = _f("q", "Which city/town do you live in?")
        assert RecruiteeSubmitter._row_for(f, RESOLVED)["domId"] == "7465192"

    def test_containment_match_for_radio_questions(self):
        """The bug that made every radio question unplaceable: the
        container's text carries its own option labels."""
        f = _f("q", "Do you live in the Netherlands?")
        assert RecruiteeSubmitter._row_for(f, RESOLVED)["domId"] == "7465118"

    def test_trailing_required_marker_is_ignored(self):
        f = _f("q", "Which city/town do you live in? *")
        assert RecruiteeSubmitter._row_for(f, RESOLVED)["domId"] == "7465192"

    def test_no_match_returns_none(self):
        f = _f("q", "A question this form does not ask")
        assert RecruiteeSubmitter._row_for(f, RESOLVED) is None

    def test_ambiguous_match_fails_safe(self):
        """Two questions containing the text means we cannot tell which
        box the answer belongs in. Returning None makes the field
        unplaceable and the gate block — infinitely better than
        answering the wrong question."""
        ambiguous = {
            "do you live in the netherlands? yes no": {"domId": "1", "suffix": "flag", "radios": [], "values": []},
            "do you live in the netherlands? (second) yes no": {"domId": "2", "suffix": "flag", "radios": [], "values": []},
        }
        f = _f("q", "Do you live in the Netherlands?")
        assert RecruiteeSubmitter._row_for(f, ambiguous) is None

    def test_empty_label_never_matches(self):
        assert RecruiteeSubmitter._row_for(_f("q", ""), RESOLVED) is None


class TestDomNames:
    def test_fixed_fields_map_to_candidate_names(self):
        assert _FIXED_NAMES["name"] == "candidate.name"
        assert _FIXED_NAMES["resume"] == "candidate.cv"

    def test_boolean_question_uses_flag_suffix(self):
        f = _f("q", "Do you live in the Netherlands?", "boolean", "Yes")
        assert RecruiteeSubmitter()._dom_name(f, RESOLVED).endswith(".7465118.flag")

    def test_other_questions_use_content_suffix(self):
        f = _f("q", "Which city/town do you live in?")
        assert RecruiteeSubmitter()._dom_name(f, RESOLVED).endswith(".7465192.content")

    def test_unmatched_question_has_no_dom_name(self):
        f = _f("q", "Not on this form")
        assert RecruiteeSubmitter()._dom_name(f, RESOLVED) == ""


class TestValueVerification:
    def test_boolean_yes_is_true(self):
        f = _f("q", "Q", "boolean", "Yes")
        assert RecruiteeSubmitter._value_present(f, "true")
        assert not RecruiteeSubmitter._value_present(f, "false")

    def test_boolean_no_is_false(self):
        f = _f("q", "Q", "boolean", "No")
        assert RecruiteeSubmitter._value_present(f, "false")

    def test_unchecked_radio_fails(self):
        f = _f("q", "Q", "boolean", "Yes")
        assert not RecruiteeSubmitter._value_present(f, "")

    def test_phone_compared_on_digits(self):
        """intl-tel reformats as you type and splits the country code
        into its own control, so a literal compare false-alarms."""
        f = _f("phone", "Phone", "text", "+31612345678")
        assert RecruiteeSubmitter._value_present(f, "612345678")
        assert RecruiteeSubmitter._value_present(f, "+31 6 1234 5678")

    def test_wrong_phone_still_fails(self):
        f = _f("phone", "Phone", "text", "+31612345678")
        assert not RecruiteeSubmitter._value_present(f, "999")

    def test_text_compared_on_prefix(self):
        f = _f("q", "Q", "text", "Amsterdam")
        assert RecruiteeSubmitter._value_present(f, "Amsterdam")
        assert not RecruiteeSubmitter._value_present(f, "Rotterdam")

    def test_choice_matches_option_text(self):
        f = _f("q", "Q", "select", "EU citizenship")
        assert RecruiteeSubmitter._value_present(f, "EU citizenship")
        assert not RecruiteeSubmitter._value_present(f, "Valid work permit")


class TestRadioPicking:
    @pytest.mark.parametrize("value,expected_id_suffix", [
        ("true", "flag-14-0"), ("false", "flag-14-1"),
    ])
    def test_radio_ids_are_carried_for_label_targeting(self, value, expected_id_suffix):
        row = RESOLVED["do you live in the netherlands?  yes no"]
        match = next(r for r in row["radios"] if r["value"] == value)
        assert match["id"].endswith(expected_id_suffix)

    def test_resolver_collects_radio_ids(self):
        """Needed because the label points at the radio by id, and the
        input itself cannot be clicked."""
        assert "radios:" in _RESOLVE_JS
        assert "id: i.id" in _RESOLVE_JS

    def test_readback_records_checked_radio_values(self):
        assert "e.checked" in _READBACK_JS


class TestNormalisation:
    def test_collapses_whitespace_and_strips_markers(self):
        assert _norm("  Do you  live *here? ") == "do you live here?"

    def test_handles_none(self):
        assert _norm(None) == ""
