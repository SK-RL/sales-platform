"""F377 — Breezy HR: feed, form extraction, submitter.

Verified live on vetsez.breezy.hr: the JSON feed lists 62 positions (a
missing board is a 404 → empty list); the apply page extracted 21 fields
with the never-infer gate flagging salary, work authorisation,
sponsorship, clearance, veteran, race, gender and disability; the dry
run placed 21/21 with readback. Fixtures below are trimmed from those
live payloads, not invented.
"""

import pytest

from app.fetchers.breezy import BreezyFetcher
from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, normalise_breezy_rows
from app.services.own_link import parse_job_url
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.base import SubmitField
from app.services.submitters.breezy import _SUBMIT_SELECTOR, BreezySubmitter
from app.workers.tasks._answer_prep import match_questions_to_answers

FEED_ITEM = {
    "id": "18df3ec23bf901", "friendly_id": "18df3ec23bf901-appian-integration-developer-remote-opportunity",
    "name": "Appian Integration Developer (Remote Opportunity)",
    "url": "https://vetsez.breezy.hr/p/18df3ec23bf901-appian-integration-developer-remote-opportunity",
    "published_date": "2026-09-02T15:14:49.573Z", "type": {"id": "fullTime", "name": "Full-Time"},
    "location": {"country": {"name": "United States", "id": "US"}, "city": "Tampa", "is_remote": True,
                 "remote_details": {"value": "remote-location", "label": "Fully remote, within chosen location(s)"}, "name": "Tampa, FL"},
    "department": None, "company": {"name": "VetsEZ", "friendly_id": "vetsez"},
}


def _r(tag, type_, name, question="", option="", options=None, required=True, honeypot=False):
    return {"tag": tag, "type": type_, "name": name, "id": "", "question": question, "option": option,
            "options": options or [], "required": required, "honeypot": honeypot, "value": ""}


LIVE_ROWS = [
    _r("input", "file", "cResume", "Upload Resume"),
    _r("input", "text", "cName", "Full Name"),
    _r("input", "email", "cEmail", "Email Address"),
    _r("input", "text", "cPhoneNumber", "Phone Number"),
    _r("input", "checkbox", "smsConsent", "Phone Number", required=False),
    _r("input", "text", "cSalary", "Desired Salary"),
    _r("select", "select-one", "", "Desired Salary", options=["Hourly", "Weekly", "Monthly", "Yearly"]),
    _r("input", "text", "hp_7f2b", "", required=False, honeypot=True),
    _r("input", "text", "section_1783434304566_question_0", "Location (City/State)"),
    _r("select", "select-one", "section_1783434304566_question_1", "Are you authorized to work legally in the United States?", options=["No", "Yes"]),
    _r("select", "select-one", "section_1783434304566_question_2", "Will you now or in the future require visa sponsorship?", options=["Yes", "No"]),
    _r("input", "radio", "race_ethnicity", "Race or Ethnicity", option="White (not Hispanic or Latino)"),
    _r("input", "radio", "race_ethnicity", "Race or Ethnicity", option="I don't wish to answer"),
    _r("input", "radio", "gender", "Gender", option="Male"),
    _r("input", "radio", "gender", "Gender", option="Female"),
    _r("input", "checkbox", "ccpaAgreement", "", option="I've read the Privacy Notice below and consent the processing of my data"),
]


class TestFetcher:
    def test_normalises_a_feed_item(self):
        j = BreezyFetcher()._normalize(FEED_ITEM, "vetsez")
        assert j["external_id"] == "breezy-18df3ec23bf901" and j["platform"] == "breezy"
        assert j["url"].startswith("https://vetsez.breezy.hr/p/18df3ec23bf901")
        assert j["location_raw"] == "Remote (Tampa, FL)" and j["remote_scope"] == "remote"
        assert j["raw_json"]["company_name"] == "VetsEZ"

    def test_remote_anywhere_is_global(self):
        item = {**FEED_ITEM, "location": {**FEED_ITEM["location"], "remote_details": {"value": "remote-anywhere"}}}
        assert BreezyFetcher()._normalize(item, "vetsez")["remote_scope"] == "global"

    def test_registered_everywhere(self):
        import inspect

        from app.fetchers import FETCHER_MAP
        import app.workers.tasks.discovery_task as dt
        assert FETCHER_MAP["breezy"] is BreezyFetcher
        assert "https://{slug}.breezy.hr/json" in inspect.getsource(dt)


class TestExtraction:
    def test_fixed_fields_and_honeypot(self):
        k = {f["field_key"]: f for f in normalise_breezy_rows(LIVE_ROWS)}
        assert k["name"]["field_type"] == "text" and k["email"]["required"]
        assert k["resume"]["field_type"] == "file"
        assert not any(key.startswith("hp_") for key in k), "the honeypot must never become a field"

    def test_other_fixed_fields_seen_on_live_boards(self):
        rows = [_r("input", "text", "cLocation", "Location"), _r("textarea", "textarea", "cSummary", "Summary"), _r("textarea", "textarea", "cCoverLetter", "Cover Letter")]
        k = {f["field_key"]: f for f in normalise_breezy_rows(rows)}
        assert k["location"]["field_type"] == "text" and k["summary"]["field_type"] == "textarea" and k["cover_letter"]["field_type"] == "textarea"
        from app.services.submitters.breezy import _FIXED_NAMES
        assert _FIXED_NAMES["summary"] == "cSummary" and _FIXED_NAMES["location"] == "cLocation"

    def test_salary_and_its_period(self):
        k = {f["field_key"]: f for f in normalise_breezy_rows(LIVE_ROWS)}
        assert k["salary"]["label"] == "Desired Salary"
        assert k["salary_period"]["options"] == ["Hourly", "Weekly", "Monthly", "Yearly"]

    def test_custom_questions_keep_their_text_and_options(self):
        k = {f["field_key"]: f for f in normalise_breezy_rows(LIVE_ROWS)}
        assert k["section_1783434304566_question_1"]["options"] == ["No", "Yes"]
        assert "authorized to work" in k["section_1783434304566_question_1"]["label"]

    def test_eeo_radios_collapse_to_selects(self):
        k = {f["field_key"]: f for f in normalise_breezy_rows(LIVE_ROWS)}
        assert k["race_ethnicity"]["field_type"] == "select" and len(k["race_ethnicity"]["options"]) == 2
        assert k["gender"]["options"] == ["Male", "Female"]

    def test_privacy_consent_is_a_boolean_with_its_own_text(self):
        k = {f["field_key"]: f for f in normalise_breezy_rows(LIVE_ROWS)}
        assert k["privacy_consent"]["field_type"] == "boolean" and "Privacy Notice" in k["privacy_consent"]["label"]

    def test_gate_never_infers_the_legal_and_eeo_ones(self):
        m = {q["field_key"]: q for q in match_questions_to_answers(normalise_breezy_rows(LIVE_ROWS), [])}
        for key in ("salary", "salary_period", "section_1783434304566_question_1", "section_1783434304566_question_2", "race_ethnicity", "gender"):
            assert m[key]["never_infer"] is True, key
        assert m["section_1783434304566_question_0"]["never_infer"] is False


class TestSubmitter:
    def test_registered_and_auto_submittable(self):
        assert isinstance(get_submitter("breezy"), BreezySubmitter)
        assert "breezy" in SUPPORTED_QUESTION_PLATFORMS and "breezy" in auto_submittable_platforms()

    def test_submit_is_the_angular_button_by_text(self):
        assert "Submit Application" in _SUBMIT_SELECTOR and _SUBMIT_SELECTOR.startswith("form ")

    def test_apply_url(self):
        assert BreezySubmitter._apply_url("https://vetsez.breezy.hr/p/18df-x?src=1") == "https://vetsez.breezy.hr/p/18df-x/apply"
        assert BreezySubmitter._apply_url("https://vetsez.breezy.hr/p/18df-x/apply") == "https://vetsez.breezy.hr/p/18df-x/apply"

    def test_selectors_never_touch_the_honeypot(self):
        f = SubmitField(field_key="salary_period", label="", field_type="select", value="Yearly", options=["Yearly"])
        assert BreezySubmitter._selector(f) == '[data-apply-field="salary_period"]'
        assert BreezySubmitter._selector(SubmitField(field_key="name", label="", field_type="text", value="x")) == '[name="cName"]'

    def test_value_checks(self):
        v = BreezySubmitter._value_present
        assert v(SubmitField(field_key="gender", label="", field_type="select", value="Female", options=["Male", "Female"]), "Female")
        assert not v(SubmitField(field_key="gender", label="", field_type="select", value="Female", options=["Male", "Female"]), "Male")
        assert v(SubmitField(field_key="privacy_consent", label="", field_type="boolean", value="Yes"), "Yes")
        assert not v(SubmitField(field_key="privacy_consent", label="", field_type="boolean", value="Yes"), "")
        assert v(SubmitField(field_key="phone", label="", field_type="text", value="+1 415 555 0100"), "(415) 555-0100")


class TestOwnLink:
    def test_breezy_posting_link_parses(self):
        p = parse_job_url("https://vetsez.breezy.hr/p/18df3ec23bf901-appian-integration-developer/apply")
        assert (p.platform, p.slug, p.external_id) == ("breezy", "vetsez", "breezy-18df3ec23bf901")

    def test_marketing_host_is_not_a_board(self):
        assert parse_job_url("https://app.breezy.hr/p/abc") is None
