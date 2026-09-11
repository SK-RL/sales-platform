"""F390 Hireology (scan, extract, submit).

Public JSON both ways: the careers list at api.hireology.com (paged,
page_size up to 100) and the application-form schema per job. The form
sits on the posting page with no captcha; controls are ``#{field}-0``.
Verified live on familiarroadshomehealthcareagency (35 postings).
"""

from app.fetchers import FETCHER_MAP
from app.fetchers.hireology import HireologyFetcher
from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, normalise_hireology_form
from app.services.own_link import parse_job_url
from app.services.company_lookup import PROBE_PLATFORMS, canonical_posting_url
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.base import SubmitField
from app.services.submitters.hireology import HireologySubmitter

POSTING = {"id": 2555744, "name": "Direct Care Worker (DCW)", "status": "Open", "employment_status": "Full Time - hourly",
           "locations": [{"city": "Wilkes-Barre", "state": "PA", "zip_code": "18701"}], "remote": False,
           "career_site_path": "/familiarroadshomehealthcareagency/2555744/description", "job_family": {"name": "General"},
           "organization": {"name": "Familiar Roads"}, "created_at": "2025-07-09T16:08:21.001Z",
           "compensation": {"comp_range_min": "13.0", "comp_range_max": "16.0", "comp_period": "hour"}}

FORM = {"form_title": "Apply for Direct Care Worker (DCW)", "job": {"id": 2555744, "careers_pathname": "familiarroadshomehealthcareagency"}, "template": {"sections": [
    {"id": "basic", "fieldsets": [
        {"id": "name", "fields": [{"id": "first_name", "attributes": {"type": "text", "required": True}}, {"id": "last_name", "attributes": {"type": "text", "required": True}}]},
        {"id": "email", "fields": [{"id": "email_address", "attributes": {"type": "email", "required": True}}]},
        {"id": "phone_number", "fields": [{"id": "home_phone", "attributes": {"type": "tel", "required": True}}]},
        {"id": "sms_opt_in", "fields": [{"id": "sms_opt_in", "attributes": {"type": "checkbox", "required": True, "checked": True}}]},
        {"id": "address", "fields": [{"id": "street_address", "attributes": {"type": "text", "required": False}}, {"id": "state_id", "attributes": {"type": "select", "required": False, "options": [{"name": "AK", "value": 2}, {"name": "--", "value": 52}, {"name": "PA", "value": 39}]}}]},
        {"id": "referred_by", "fields": [{"id": "candidate_referred", "attributes": {"type": "radio", "required": False, "options": [{"name": "Yes", "value": True}, {"name": "No", "value": False}]}}, {"id": "referred_by", "attributes": {"type": "text", "required": False}}]},
        {"id": "resume", "fields": [{"id": "resume", "attributes": {"type": "file", "required": True}}]},
    ]},
    {"id": "questions", "fieldsets": [{"id": "q1", "label": "Are you legally authorized to work in the US?", "fields": [{"id": "custom_123", "attributes": {"type": "radio", "required": True, "options": [{"name": "Yes", "value": 1}, {"name": "No", "value": 0}]}}]}]},
]}}


class TestHireology:
    def test_feed_item(self):
        j = HireologyFetcher()._normalize(POSTING, "familiarroadshomehealthcareagency")
        assert j["external_id"] == "hireology-2555744" and j["url"].endswith("/2555744/description")
        assert j["location_raw"] == "Wilkes-Barre, PA" and j["department"] == "General" and j["salary_range"] == "13.0-16.0 per hour"
        assert HireologyFetcher()._normalize({**POSTING, "status": "Closed"}, "x") is None
        assert HireologyFetcher()._normalize({**POSTING, "remote": True}, "x")["location_raw"] == "Remote (Wilkes-Barre, PA)"

    def test_extraction(self):
        k = {f["field_key"]: f for f in normalise_hireology_form(FORM)}
        assert k["first_name"]["required"] and k["email"]["field_type"] == "text" and k["phone"]["label"] == "Phone number"
        assert k["sms_opt_in"]["field_type"] == "boolean" and k["sms_opt_in"]["required"] is False
        assert k["state"]["options"] == ["AK", "PA"]  # the '--' separator is not an option
        assert k["candidate_referred"]["field_type"] == "select" and k["candidate_referred"]["options"] == ["Yes", "No"]
        assert k["resume"]["field_type"] == "file" and k["resume"]["required"]
        q = k["custom_123"]
        assert q["label"] == "Are you legally authorized to work in the US?" and q["field_type"] == "select" and q["required"] and q["options"] == ["Yes", "No"]

    def test_registered_everywhere(self):
        assert FETCHER_MAP["hireology"] is HireologyFetcher
        assert isinstance(get_submitter("hireology"), HireologySubmitter)
        assert "hireology" in SUPPORTED_QUESTION_PLATFORMS and "hireology" in auto_submittable_platforms()
        assert "hireology" in PROBE_PLATFORMS

    def test_own_link_and_canonical(self):
        p = parse_job_url("https://careers.hireology.com/familiarroadshomehealthcareagency/2555744/description")
        assert (p.platform, p.slug, p.external_id) == ("hireology", "familiarroadshomehealthcareagency", "hireology-2555744")
        assert parse_job_url("https://careers.hireology.com/acme/12?x=1").external_id == "hireology-12"
        assert parse_job_url("https://careers.hireology.com/acme") is None
        assert canonical_posting_url("hireology", "acme", {"external_id": "hireology-12", "url": ""}) == "https://careers.hireology.com/acme/12/description"

    def test_submitter_ids_and_readback(self):
        assert HireologySubmitter._dom_id(SubmitField(field_key="email", label="Email", field_type="text", value="a@b.c")) == "email_address"
        assert HireologySubmitter._dom_id(SubmitField(field_key="custom_123", label="Q", field_type="select", value="Yes")) == "custom_123"
        f = SubmitField(field_key="sms_opt_in", label="SMS", field_type="boolean", value="No")
        assert HireologySubmitter._value_present(f, "No") and not HireologySubmitter._value_present(f, "Yes")
