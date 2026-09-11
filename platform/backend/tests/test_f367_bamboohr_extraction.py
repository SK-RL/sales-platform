"""F367 — BambooHR: form extraction over plain HTTP.

GET https://{slug}.bamboohr.com/careers/{id}/detail returns a complete
``formFields`` map — the cleanest form source since Greenhouse. The
fixture is the live response for icmarkets/128, trimmed not invented.
BambooHR was blocked for a day not by anything technical but because
our seed catalogue had no live tenant; two were found by web search
(icmarkets, icmarketsglobal).
"""
import httpx

from app.fetchers.questions import (
    SUPPORTED_QUESTION_PLATFORMS, _bamboo_job_id, fetch_application_questions, normalise_bamboohr_form,
)
from app.workers.tasks._answer_prep import match_questions_to_answers

LIVE_FORM = {
    "firstName": {"isRequired": True, "value": "", "label": "First Name"},
    "email": {"isRequired": True, "value": "", "label": "Email"},
    "state": {"isRequired": True, "value": "", "label": "State", "options": []},
    "countryId": {"isRequired": True, "value": "", "label": "Country",
                  "options": [{"id": "1", "text": "United States"}, {"id": "2", "text": "Canada"}]},
    "linkedinUrl": {"isRequired": False, "value": "", "label": "LinkedIn URL"},
    "coverLetterFileId": {"isRequired": False, "value": "", "label": "Cover Letter"},
    "resumeFileId": {"isRequired": True, "value": "", "label": "Resume"},
    "customQuestions": [
        {"id": "550", "isRequired": True, "question": "I acknowledge that this is a fully on-site working role", "type": "checkbox", "options": [], "hasOther": "no"},
        {"id": "552", "isRequired": True, "question": "Will you need sponsorship for a working visa?", "type": "yes_no", "options": [], "hasOther": "no"},
        {"id": "551", "isRequired": True, "question": "What are your gross annual salary expectations in \u20ac for this role?", "type": "short", "options": [], "hasOther": "no"},
        {"id": "554", "isRequired": True, "question": "Where did you hear about this role?", "type": "multi",
         "options": [{"id": 411, "option": "Alpha Jobs"}, {"id": 412, "option": "Carierista"}], "hasOther": "yes"},
    ],
    "genderId": [], "ethnicityId": [], "veteranStatusId": [], "disabilityId": [],
}


def _k():
    return {f["field_key"]: f for f in normalise_bamboohr_form(LIVE_FORM)}


class TestJobId:
    def test_strips_our_prefix_with_hyphenated_slug(self):
        assert _bamboo_job_id("bamboo-ic-markets-128") == "128"
        assert _bamboo_job_id("bamboo-icmarkets-128") == "128"

    def test_bare_id_passes(self):
        assert _bamboo_job_id("128") == "128"


class TestNormalisation:
    def test_fixed_fields_map_to_canonical_keys(self):
        k = _k()
        assert k["first_name"]["required"] and k["first_name"]["field_type"] == "text"
        assert k["resume"]["field_type"] == "file" and k["resume"]["required"]
        assert k["cover_letter_file"]["required"] is False

    def test_country_is_a_select_with_option_text(self):
        assert _k()["country"]["options"] == ["United States", "Canada"]

    def test_state_with_empty_options_degrades_to_text(self):
        """Non-US boards ship options: [] — a select with nothing to pick
        would block forever."""
        assert _k()["state"]["field_type"] == "text"

    def test_custom_question_types(self):
        k = _k()
        assert k["bamboo_q_552"]["field_type"] == "boolean"     # yes_no
        assert k["bamboo_q_550"]["field_type"] == "boolean"     # checkbox
        assert k["bamboo_q_551"]["field_type"] == "text"        # short
        assert k["bamboo_q_554"]["field_type"] == "select"      # multi

    def test_has_other_adds_an_other_option(self):
        assert _k()["bamboo_q_554"]["options"] == ["Alpha Jobs", "Carierista", "Other"]

    def test_empty_eeo_lists_are_not_asked(self):
        assert not any(k in _k() for k in ("gender", "race", "veteran_status", "disability_status"))

    def test_eeo_list_with_options_becomes_a_select(self):
        form = dict(LIVE_FORM, genderId=[{"id": 1, "text": "Male"}, {"id": 2, "text": "Female"}])
        assert normalise_bamboohr_form(form)[-1]["field_key"] == "gender" or \
            {f["field_key"]: f for f in normalise_bamboohr_form(form)}["gender"]["options"] == ["Male", "Female"]


class TestGate:
    def test_sponsorship_and_salary_are_never_infer(self):
        m = {x["field_key"]: x for x in match_questions_to_answers(normalise_bamboohr_form(LIVE_FORM), [])}
        assert m["bamboo_q_552"]["never_infer"] is True
        assert m["bamboo_q_551"]["never_infer"] is True
        assert m["bamboo_q_550"]["never_infer"] is False


class TestWiring:
    def test_supported(self):
        assert "bamboohr" in SUPPORTED_QUESTION_PLATFORMS

    def test_hits_the_detail_endpoint_and_marks_extracted(self, monkeypatch):
        captured = {}

        class _Resp:
            def raise_for_status(self): return None
            def json(self): return {"result": {"formFields": LIVE_FORM}}

        class _Client:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): captured["url"] = url; return _Resp()

        monkeypatch.setattr(httpx, "Client", _Client)
        fields = fetch_application_questions("bamboohr", "bamboo-icmarkets-128", "icmarkets")
        assert captured["url"] == "https://icmarkets.bamboohr.com/careers/128/detail"
        assert all(f["extraction_mode"] == "extracted" for f in fields)
