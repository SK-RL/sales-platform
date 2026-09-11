"""F387 Pinpoint (scan, extract, submit).

Verified live on made-tech.pinpointhq.com: public ``postings.json`` (23
postings), the application form is revealed by the posting page's Apply
control and is a plain Rails form with no captcha; 20+ fields extracted
including EEO questions (flagged never-infer by the gate). Enhanced
dropdowns without a native ``name`` are surfaced as ``pinpoint_*`` keys
and reported unplaceable by the submitter — the gate stops for the user
rather than guessing.
"""

from app.fetchers import FETCHER_MAP
from app.fetchers.pinpoint import PinpointFetcher
from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, normalise_pinpoint_rows
from app.services.own_link import parse_job_url
from app.services.company_lookup import PROBE_PLATFORMS, canonical_posting_url
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.base import SubmitField
from app.services.submitters.pinpoint import PinpointSubmitter

POSTING = {
    "id": 143889, "title": "Lead DevOps Engineer", "url": "https://made-tech.pinpointhq.com/en/postings/1d0e0b4a-1111-2222-3333-444455556666",
    "path": "/en/postings/1d0e0b4a-1111-2222-3333-444455556666",
    "location": {"name": "", "city": "", "province": ""}, "workplace_type_text": "Remote",
    "job": {"department": {"name": "Engineering"}}, "compensation": "£80k", "compensation_visible": True,
}


def _r(tag, type_, name, label="", question="", required=False, options=None, key="", value=""):
    return {"tag": tag, "type": type_, "name": name, "label": label, "question": question, "required": required,
            "options": options or [], "key": key, "value": value}


ROWS = [
    _r("input", "text", "application_form[application][first_name]", "First name", "First name", True),
    _r("input", "email", "application_form[application][email]", "Email Address", "Email Address", True),
    _r("input", "file", "application_form[application][cv]", "CV", "CV", True),
    _r("textarea", "", "application_form[application][summary]", "Personal Summary", "Personal Summary"),
    _r("input", "radio", "application_form[application][answers_attributes][0][boolean_answer]", "Yes", "Do you have the right to work in the UK?", True),
    _r("input", "radio", "application_form[application][answers_attributes][0][boolean_answer]", "No", "Do you have the right to work in the UK?", True),
    _r("textarea", "", "application_form[application][answers_attributes][1][text_answer]", "", "What is your salary expectation?", True),
    _r("hidden", "hidden", "application_form[application][answers_attributes][0][title]", value="Do you require any reasonable adjustments?"),
    _r("hidden", "hidden", "application_form[application][answers_attributes][0][question_type]", value="boolean"),
    _r("select", "select-one", "", "", "What pronouns do you use?", True, ["She/her", "He/him"], key="application_form[application][answers_attributes][2][choice]"),
    _r("select", "select-one", "", "", "Country", True, ["United Kingdom", "France"], key="country"),
    _r("select", "select-one", "", "", "Gender", False, ["Male", "Female", "Prefer not to say"], key="equality_monitoring[Gender]"),
    _r("select", "select-one", "", "", "Gender", False, ["Male", "Female", "Prefer not to say"], key="equality_monitoring[Gender]"),
    _r("select", "select-one", "", "", "State", True, ["Alaska", "Texas"], key="state"),
    _r("select", "select-one", "", "", "Mystery widget", False, ["A", "B"]),
]


class TestPinpoint:
    def test_feed_item(self):
        j = PinpointFetcher()._normalize(POSTING, "made-tech")
        assert j["external_id"] == "pinpoint-1d0e0b4a-1111-2222-3333-444455556666" and j["raw_json"]["feed_id"] == "143889" and j["remote_scope"] == "remote" and j["location_raw"] == "Remote"
        assert j["department"] == "Engineering" and j["salary_range"] == "£80k"

    def test_feed_item_without_url_is_dropped(self):
        assert PinpointFetcher()._normalize({**POSTING, "url": ""}, "made-tech") is None

    def test_extraction(self):
        rows = normalise_pinpoint_rows(ROWS)
        k = {f["field_key"]: f for f in rows}
        assert k["first_name"]["required"] and k["email"]["field_type"] == "text" and k["resume"]["field_type"] == "file"
        assert k["summary"]["field_type"] == "textarea"
        b = k["application_form[application][answers_attributes][0][boolean_answer]"]
        assert b["field_type"] == "boolean" and b["required"]
        assert b["label"] == "Do you require any reasonable adjustments?"  # the hidden [N][title] wins over the DOM walk
        t = k["application_form[application][answers_attributes][1][text_answer]"]
        assert t["field_type"] == "textarea" and t["label"].startswith("What is your salary")
        g = k["equality_monitoring[Gender]"]
        assert g["field_type"] == "select" and g["label"] == "Gender" and g["options"] == ["Male", "Female", "Prefer not to say"]
        assert len([f for f in rows if f["field_key"] == "equality_monitoring[Gender]"]) == 1
        c = k["application_form[application][answers_attributes][2][choice]"]
        assert c["field_type"] == "select" and c["label"] == "What pronouns do you use?" and c["required"] and c["options"] == ["She/her", "He/him"]
        assert k["country"]["required"] and k["country"]["label"] == "Country"
        assert k["state"]["required"] is False and k["state"]["description"]  # conditional on country, never blocks
        assert k["pinpoint_mystery_widget"]["options"] == ["A", "B"]  # unaddressable dropdown still surfaces so the gate asks
        assert "authenticity_token" not in k and not any(f["field_key"].endswith("[title]") for f in rows)

    def test_registered_everywhere(self):
        assert FETCHER_MAP["pinpoint"] is PinpointFetcher
        assert isinstance(get_submitter("pinpoint"), PinpointSubmitter)
        assert "pinpoint" in SUPPORTED_QUESTION_PLATFORMS and "pinpoint" in auto_submittable_platforms()
        assert "pinpoint" in PROBE_PLATFORMS

    def test_own_link(self):
        p = parse_job_url("https://made-tech.pinpointhq.com/en/postings/1d0e0b4a-1111-2222-3333-444455556666?src=x")
        assert (p.platform, p.slug, p.external_id) == ("pinpoint", "made-tech", "pinpoint-1d0e0b4a-1111-2222-3333-444455556666")
        assert parse_job_url("https://coforma.pinpointhq.com/postings/1d0e0b4a-1111-2222-3333-444455556666").slug == "coforma"
        assert parse_job_url("https://www.pinpointhq.com/postings/1d0e0b4a-1111-2222-3333-444455556666") is None

    def test_canonical_url(self):
        assert canonical_posting_url("pinpoint", "made-tech", {"external_id": "pinpoint-abc", "url": ""}) == "https://made-tech.pinpointhq.com/en/postings/abc"

    def test_select_keys_pass_through_and_unknown_dropdown_stays_unplaceable(self):
        assert PinpointSubmitter._dom_name(SubmitField(field_key="equality_monitoring[Gender]", label="Gender", field_type="select", value="Male")) == "equality_monitoring[Gender]"
        assert PinpointSubmitter._sel("email") == 'form [name="application_form[application][email]"]'
        f = SubmitField(field_key="equality_monitoring[Gender]", label="Gender", field_type="select", value="Prefer not to say", options=["Male", "Prefer not to say"])
        assert PinpointSubmitter._value_present(f, "Prefer not to say") and not PinpointSubmitter._value_present(f, "Select...")

    def test_boolean_readback(self):
        f = SubmitField(field_key="application_form[application][answers_attributes][0][boolean_answer]", label="Right to work?", field_type="boolean", value="Yes")
        assert PinpointSubmitter._value_present(f, "Yes") and not PinpointSubmitter._value_present(f, "")
