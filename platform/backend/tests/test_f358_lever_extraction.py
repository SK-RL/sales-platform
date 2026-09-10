"""F358 — read Lever's real application form.

F357 removed the old Lever "extractor" because it never read a form: it
returned a template and turned job-description headings into questions.
This replaces it with genuine extraction.

Lever's postings API really doesn't expose the form, but the apply page
is server-rendered — a plain GET of ``/{slug}/{id}/apply`` returns the
complete markup (740KB on a live posting), so no browser is needed.

The fixture below is trimmed from a live page
(jobs.lever.co/matchgroup/7fca4a70-…/apply), not invented. Against the
full page the extractor pulls 19 real fields, including the questions
that actually matter to the gate — visa sponsorship, current salary,
salary expectation, and an EEO gender survey. The old code produced
"Key Responsibilities" instead.
"""

import httpx
import pytest

from app.fetchers.questions import (
    SUPPORTED_QUESTION_PLATFORMS,
    _fetch_lever_questions,
    fetch_application_questions,
)
from app.workers.tasks._answer_prep import blocking_gaps, match_questions_to_answers

# Trimmed from the live apply page. Structure preserved exactly: each
# field is an <li class="application-question"> with a .application-label
# and its inputs; required is marked with the ✱ glyph in the label.
LEVER_HTML = """
<html><body><form>
  <li class="application-question">
    <div class="application-label">Resume/CV ✱</div>
    <input type="file" name="resume" required>
  </li>
  <li class="application-question">
    <div class="application-label">Full name ✱</div>
    <input type="text" name="name" required>
  </li>
  <li class="application-question">
    <div class="application-label">Pronouns</div>
    <label><input type="checkbox" name="pronouns" value="He/him">He/him</label>
    <label><input type="checkbox" name="pronouns" value="She/her">She/her</label>
    <label><input type="checkbox" name="pronouns" value="They/them">They/them</label>
  </li>
  <li class="application-question">
    <div class="application-label">Email ✱</div>
    <input type="email" name="email" required>
  </li>
  <li class="application-question">
    <div class="application-label">LinkedIn URL</div>
    <input type="text" name="urls[LinkedIn]">
  </li>
  <li class="application-question">
    <div class="application-label">Do you now or will you in the future require sponsorship? ✱</div>
    <label><input type="radio" name="cards[3efb0e95][0]" value="Yes" required>Yes</label>
    <label><input type="radio" name="cards[3efb0e95][0]" value="No" required>No</label>
  </li>
  <li class="application-question">
    <div class="application-label">What is your salary expectation for this position? ✱</div>
    <textarea name="cards[3efb0e95][1]" required></textarea>
  </li>
  <li class="application-question">
    <div class="application-label">What is your notice period? ✱</div>
    <textarea name="cards[3efb0e95][2]" required></textarea>
  </li>
  <li class="application-question">
    <div class="application-label">What gender do you identify as?</div>
    <label><input type="radio" name="surveysResponses[a025f000]" value="Female">Female</label>
    <label><input type="radio" name="surveysResponses[a025f000]" value="Male">Male</label>
    <label><input type="radio" name="surveysResponses[a025f000]" value="Prefer not to answer">Prefer not to answer</label>
  </li>
</form></body></html>
"""


@pytest.fixture
def stub_lever(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        text = LEVER_HTML

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **kw):
            captured["headers"] = kw.get("headers")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            captured["url"] = url
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    return captured


def _by_key(stub=None):
    return {f["field_key"]: f for f in _fetch_lever_questions("POST-1", "acme")}


class TestItReadsTheApplyPage:
    def test_hits_the_apply_url(self, stub_lever):
        _fetch_lever_questions("POST-1", "acme")
        assert stub_lever["url"] == "https://jobs.lever.co/acme/POST-1/apply"

    def test_sends_a_browser_user_agent(self, stub_lever):
        """Lever gates plain scripted clients."""
        _fetch_lever_questions("POST-1", "acme")
        assert "Mozilla" in (stub_lever["headers"] or {}).get("User-Agent", "")

    def test_lever_is_supported_again(self):
        assert "lever" in SUPPORTED_QUESTION_PLATFORMS

    def test_dispatcher_marks_it_extracted(self, stub_lever):
        fields = fetch_application_questions("lever", "POST-1", "acme")
        assert all(f["extraction_mode"] == "extracted" for f in fields)


class TestFieldShapes:
    def test_fixed_identity_fields(self, stub_lever):
        k = _by_key()
        assert k["name"]["field_type"] == "text"
        assert k["email"]["field_type"] == "text"
        assert k["resume"]["field_type"] == "file"

    def test_url_fields_keep_their_bracket_names(self, stub_lever):
        """field_key IS the input name, so the schema and any future
        submitter address the same thing."""
        assert "urls[LinkedIn]" in _by_key()

    def test_required_is_read_from_the_glyph(self, stub_lever):
        k = _by_key()
        assert k["name"]["required"] is True
        assert k["urls[LinkedIn]"]["required"] is False

    def test_radio_group_becomes_a_select_with_options(self, stub_lever):
        f = _by_key()["cards[3efb0e95][0]"]
        assert f["field_type"] == "select"
        assert f["options"] == ["Yes", "No"]

    def test_repeated_checkboxes_become_multi_select(self, stub_lever):
        f = _by_key()["pronouns"]
        assert f["field_type"] == "multi_select"
        assert "They/them" in f["options"]

    def test_textarea_card_is_a_textarea(self, stub_lever):
        assert _by_key()["cards[3efb0e95][1]"]["field_type"] == "textarea"

    def test_eeo_survey_is_captured(self, stub_lever):
        f = _by_key()["surveysResponses[a025f000]"]
        assert f["options"] == ["Female", "Male", "Prefer not to answer"]


class TestNoLongerFabricates:
    def test_jd_headings_are_not_fields(self, stub_lever):
        """The F357 regression: 'Key Responsibilities' and friends were
        presented to the candidate as questions to answer."""
        keys = set(_by_key())
        for invented in ("key_responsibilities", "required_qualifications", "work_arrangement"):
            assert invented not in keys

    def test_every_field_traces_to_a_real_input(self, stub_lever):
        """A field_key that isn't an input name can't be filled, so it
        has no business being in the schema."""
        for key in _by_key():
            assert f'name="{key}"' in LEVER_HTML


class TestGateBehaviourOnRealLeverFields:
    """The point of extracting properly: the gate can now see the
    questions that actually carry legal weight."""

    def test_sponsorship_question_is_never_infer(self, stub_lever):
        m = {q["field_key"]: q for q in match_questions_to_answers(
            _fetch_lever_questions("POST-1", "acme"), [])}
        assert m["cards[3efb0e95][0]"]["never_infer"] is True

    def test_salary_question_is_never_infer(self, stub_lever):
        m = {q["field_key"]: q for q in match_questions_to_answers(
            _fetch_lever_questions("POST-1", "acme"), [])}
        assert m["cards[3efb0e95][1]"]["never_infer"] is True

    def test_eeo_question_is_never_infer(self, stub_lever):
        m = {q["field_key"]: q for q in match_questions_to_answers(
            _fetch_lever_questions("POST-1", "acme"), [])}
        assert m["surveysResponses[a025f000]"]["never_infer"] is True

    def test_an_unanswered_form_blocks(self, stub_lever):
        matched = match_questions_to_answers(_fetch_lever_questions("POST-1", "acme"), [])
        gaps = blocking_gaps(matched, satisfied_field_keys={"resume"})
        assert gaps
        assert any("legal or protected-class" in g["reason"] for g in gaps)

    def test_lever_still_has_no_submitter(self, stub_lever):
        """Extraction and submission are separate capabilities. Reading
        Lever's form does not mean we can drive it."""
        from app.services.submitters import auto_submittable_platforms, submit_platforms

        assert "lever" not in submit_platforms()
        assert "lever" not in auto_submittable_platforms()
