"""F350 — several ATS fields can be alternatives for one requirement.

Found by dry-running the adapter against a live Greenhouse posting
(Figma 5426468004). The run failed with:

    required fields could not be located in the form: resume_text

The Job Board API returns ONE question, "Resume/CV" (required), carrying
TWO fields:

    field name='resume'      type='input_file'
    field name='resume_text' type='textarea'

They are alternatives — attach a file *or* paste the text. Our extractor
flattened them into two independently-required fields, so:

  * the gate demanded an answer for `resume_text` and blocked every
    application that had a perfectly good resume attached, and
  * the adapter aborted, because `resume_text` isn't even rendered until
    you choose "enter manually" in the UI.

Fields sharing an ``alternative_group`` now count as one requirement.
"""

import httpx
import pytest

from app.fetchers.questions import _fetch_greenhouse_questions
from app.services.submitters import SubmitField
from app.workers.tasks._answer_prep import blocking_gaps, match_questions_to_answers

# The real API shape, trimmed to the two questions that matter.
GH_PAYLOAD = {
    "questions": [
        {
            "label": "Resume/CV",
            "required": True,
            "fields": [
                {"name": "resume", "type": "input_file"},
                {"name": "resume_text", "type": "textarea"},
            ],
        },
        {
            "label": "First Name",
            "required": True,
            "fields": [{"name": "first_name", "type": "input_text"}],
        },
    ]
}


@pytest.fixture
def stub_gh(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return GH_PAYLOAD

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)


class TestExtractorTagsAlternatives:
    def test_multi_field_question_gets_a_shared_group(self, stub_gh):
        by_key = {f["field_key"]: f for f in _fetch_greenhouse_questions("1", "acme")}
        assert by_key["resume"]["alternative_group"]
        assert (
            by_key["resume"]["alternative_group"]
            == by_key["resume_text"]["alternative_group"]
        )

    def test_single_field_question_has_no_group(self, stub_gh):
        by_key = {f["field_key"]: f for f in _fetch_greenhouse_questions("1", "acme")}
        assert by_key["first_name"]["alternative_group"] == ""

    def test_both_alternatives_still_report_required(self, stub_gh):
        """The requirement is real — it just belongs to the group, not to
        each field individually."""
        by_key = {f["field_key"]: f for f in _fetch_greenhouse_questions("1", "acme")}
        assert by_key["resume"]["required"] is True
        assert by_key["resume_text"]["required"] is True


class TestGateTreatsAGroupAsOneRequirement:
    QUESTIONS = [
        {
            "field_key": "resume",
            "label": "Resume/CV",
            "field_type": "file",
            "required": True,
            "options": [],
            "alternative_group": "altgroup_resumecv",
        },
        {
            "field_key": "resume_text",
            "label": "Resume/CV",
            "field_type": "textarea",
            "required": True,
            "options": [],
            "alternative_group": "altgroup_resumecv",
        },
    ]

    def test_unsatisfied_group_still_blocks(self):
        matched = match_questions_to_answers(self.QUESTIONS, [])
        assert blocking_gaps(matched)

    def test_uploaded_resume_satisfies_the_whole_group(self):
        """apply_task declares `resume` satisfied because it uploads the
        stored file rather than typing it."""
        matched = match_questions_to_answers(self.QUESTIONS, [])
        assert blocking_gaps(matched, satisfied_field_keys={"resume"}) == []

    def test_a_typed_answer_on_either_member_satisfies_it(self):
        matched = match_questions_to_answers(
            self.QUESTIONS,
            [{"question_key": "resume_text", "answer": "pasted CV", "category": "", "source": "manual"}],
        )
        assert blocking_gaps(matched) == []

    def test_ungrouped_required_field_is_unaffected(self):
        solo = [{
            "field_key": "first_name", "label": "First Name",
            "field_type": "text", "required": True, "options": [],
            "alternative_group": "",
        }]
        matched = match_questions_to_answers(solo, [])
        assert len(blocking_gaps(matched)) == 1

    def test_satisfied_keys_do_not_leak_across_groups(self):
        qs = self.QUESTIONS + [{
            "field_key": "cover_letter_text", "label": "Cover Letter",
            "field_type": "textarea", "required": True, "options": [],
            "alternative_group": "altgroup_coverletter",
        }]
        matched = match_questions_to_answers(qs, [])
        gaps = blocking_gaps(matched, satisfied_field_keys={"resume"})
        assert [g["field_key"] for g in gaps] == ["cover_letter_text"]


class TestSubmitFieldCarriesTheGroup:
    def test_defaults_to_empty(self):
        f = SubmitField(field_key="x", label="X", field_type="text", value="v")
        assert f.alternative_group == ""

    def test_can_be_set(self):
        f = SubmitField(
            field_key="resume_text", label="Resume/CV", field_type="textarea",
            value="", alternative_group="altgroup_resumecv",
        )
        assert f.alternative_group == "altgroup_resumecv"
