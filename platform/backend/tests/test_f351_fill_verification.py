"""F351 — never trust ``fill()``; read the DOM back.

Found by writing a readback harness against a live Greenhouse posting.
The adapter reported all fields placed. Three of them were empty:

    field        claimed  intended            actual in DOM
    first_name   True     Testy               (blank)   <-- MISMATCH
    last_name    True     Readback            (blank)   <-- MISMATCH
    email        True     probe@example.com   (blank)   <-- MISMATCH

Cause: we navigated with ``domcontentloaded`` and started typing
immediately, so React hydration replaced the inputs *after* we'd set
them. Playwright raised nothing — ``fill()`` succeeded against the
pre-hydration DOM. The adapter would have submitted an application with
no name and no email on it, and reported success.

Two fixes, both pinned here:
  1. wait for the form anchor + a hydration settle before filling
  2. read every field back after filling and demote anything that
     doesn't hold what we intended

(2) is the load-bearing one: it turns this whole class of bug from
silent to loud. Re-running with the settle forced to 0 now yields
``unverified:first_name`` and a failed outcome instead of a false pass.

Also fixes ``_choice_committed``, which built malformed CSS by
interpolating an already comma-separated selector into another comma
list, then fell back to reading the first ``.select__single-value``
*anywhere on the page* — so any other combobox holding a value made it
report success for every dropdown.
"""

from app.services.submitters import SubmitField
from app.services.submitters.greenhouse import (
    _FORM_READY_SELECTOR,
    _HYDRATION_SETTLE_SECONDS,
    _READBACK_JS,
    GreenhouseSubmitter,
)


def _f(key, ftype="text", value="Testy", options=None):
    return SubmitField(
        field_key=key, label=key, field_type=ftype, value=value,
        options=options or [],
    )


class TestHydrationGuard:
    def test_form_anchor_is_waited_for(self):
        assert _FORM_READY_SELECTOR == "#first_name"

    def test_settle_is_non_zero(self):
        """Zero was the bug. A live run with settle=0 loses the first
        three fields."""
        assert _HYDRATION_SETTLE_SECONDS > 0


class TestReadbackScript:
    def test_reads_react_select_committed_value(self):
        """react-select clears the typed text on commit and renders the
        choice in a sibling node, so the input's own value is empty."""
        assert ".select__single-value" in _READBACK_JS
        assert ".select__control" in _READBACK_JS

    def test_keys_by_element_id(self):
        assert "out[e.id]" in _READBACK_JS

    def test_covers_textareas_not_just_inputs(self):
        assert "input,textarea" in _READBACK_JS


class TestValuePresent:
    """The check that decides whether a fill actually landed."""

    def test_exact_text_match_passes(self):
        assert GreenhouseSubmitter._value_present(_f("first_name"), "Testy", {})

    def test_empty_dom_value_fails(self):
        """The live regression: claimed placed, actually blank."""
        assert not GreenhouseSubmitter._value_present(_f("first_name"), "", {})

    def test_wrong_value_fails(self):
        assert not GreenhouseSubmitter._value_present(
            _f("first_name", value="Testy"), "SomethingElse", {}
        )

    def test_case_and_whitespace_tolerated(self):
        assert GreenhouseSubmitter._value_present(
            _f("email", value="Probe@Example.com"), "  probe@example.com ", {}
        )

    def test_long_text_compared_on_prefix(self):
        """Trailing normalisation (trimming, masks) must not false-alarm."""
        long = "A" * 200
        assert GreenhouseSubmitter._value_present(
            _f("q", "textarea", long), long[:120], {}
        )

    def test_empty_intended_value_is_never_present(self):
        assert not GreenhouseSubmitter._value_present(_f("q", value=""), "", {})


class TestSelectVerification:
    OPTIONS = [{"value": "1", "label": "Yes"}, {"value": "0", "label": "No"}]

    def test_matches_on_rendered_label(self):
        """Greenhouse submits value "1" but renders "Yes"; verification
        has to compare against what's on screen."""
        f = _f("q1", "select", value="Yes", options=self.OPTIONS)
        assert GreenhouseSubmitter._value_present(f, "Yes", {"q1": "Yes"})

    def test_matches_when_answer_differs_from_display(self):
        f = _f("q1", "select", value="1", options=self.OPTIONS)
        assert GreenhouseSubmitter._value_present(f, "Yes", {"q1": "Yes"})

    def test_uncommitted_select_fails(self):
        f = _f("q1", "select", value="Yes", options=self.OPTIONS)
        assert not GreenhouseSubmitter._value_present(f, "", {"q1": "Yes"})

    def test_wrong_option_committed_fails(self):
        f = _f("q1", "select", value="Yes", options=self.OPTIONS)
        assert not GreenhouseSubmitter._value_present(f, "Maybe", {"q1": "Yes"})

    def test_display_for_maps_value_to_label(self):
        assert GreenhouseSubmitter._display_for("1", self.OPTIONS) == "Yes"

    def test_display_for_passes_plain_options_through(self):
        assert GreenhouseSubmitter._display_for("SEO", ["SEO", "SEM"]) == "SEO"

    def test_display_for_unknown_value_falls_back_to_itself(self):
        assert GreenhouseSubmitter._display_for("9", self.OPTIONS) == "9"


class TestChoiceCommittedIsScoped:
    def test_query_targets_one_element_by_id(self):
        """The broken version read the first .select__single-value on the
        page, so one filled combobox made every dropdown look placed."""
        import inspect

        src = inspect.getsource(GreenhouseSubmitter._choice_committed)
        assert "getElementById" in src
        assert "closest('.select__control')" in src

    def test_takes_a_field_key_not_a_selector(self):
        import inspect

        params = inspect.signature(GreenhouseSubmitter._choice_committed).parameters
        assert "field_key" in params
