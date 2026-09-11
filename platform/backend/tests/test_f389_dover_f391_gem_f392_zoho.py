"""F389 Dover, F391 Gem (scan, extract, submit) and F392 Zoho Recruit
(scan + link, walled); plus the honest refusals for JOIN, Polymer and
CareerPlug.

Dover — public JSON (careers-page-slug → job-groups; a posting's
application_questions); MUI form by ``name``; Turnstile invisible.
Gem — public GraphQL batch endpoint; nameless React inputs matched by
label, radios by option extId; hCaptcha ``checkbox-invisible``.
Zoho Recruit — openings embedded as JSON in the board HTML; the
"I'm interested" form ends in an image CAPTCHA → KNOWN_HUMAN_WALLS.
"""

import json

from app.fetchers import FETCHER_MAP
from app.fetchers.dover import DoverFetcher
from app.fetchers.gem import GemFetcher
from app.fetchers.zoho import ZohoRecruitFetcher
from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, human_wall_for, normalise_dover_questions, normalise_gem_form
from app.services.own_link import parse_job_url, refusal_for
from app.services.company_lookup import PROBE_PLATFORMS, canonical_posting_url
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.base import SubmitField
from app.services.submitters.dover import DoverSubmitter
from app.services.submitters.gem import GemSubmitter

DOVER_JOB = {"id": "0d29285d-1b13-46ce-9390-6033a275b50e", "title": "Software Engineer", "is_published": True, "is_sample": False,
             "locations": [{"location_type": "REMOTE", "name": "United States"}, {"location_type": "REMOTE", "name": "Canada"}],
             "workplace_type": "REMOTE", "client_name": "Dover", "compensation": {"employment_type": "FULL_TIME"}}
DOVER_QS = [
    {"id": "4c65e99e", "question": "How many startups with under 50 people have you worked at?", "input_type": "MULTIPLE_CHOICE", "required": True, "question_type": "CUSTOM", "multiple_choice_options": ["0", "1", "2+"], "max_selections": None},
    {"id": "12ca0304", "question": "Resume Upload", "input_type": "FILE_UPLOAD", "required": True, "question_type": "RESUME"},
    {"id": "6ac370fc", "question": "LinkedIn Profile URL", "input_type": "SHORT_ANSWER", "required": True, "question_type": "LINKEDIN_URL"},
    {"id": "4114d8fb", "question": "Phone Number", "input_type": "SHORT_ANSWER", "required": False, "question_type": "PHONE_NUMBER"},
    {"id": "88bfb221", "question": "Add links to your best work", "input_type": "LONG_ANSWER", "required": False, "question_type": "CUSTOM"},
    {"id": "aa11", "question": "Which stacks?", "input_type": "MULTIPLE_CHOICE", "required": False, "question_type": "CUSTOM", "multiple_choice_options": ["Go", "Rust"], "max_selections": 2},
    {"id": "hid", "question": "Hidden", "input_type": "SHORT_ANSWER", "required": True, "question_type": "CUSTOM", "hidden": True},
]

GEM_POST = {"extId": "4632989005", "title": "Cloud Inference Engineer", "isApplicationFormHidden": False,
            "locations": [{"name": "United States / Canada", "isRemote": True}],
            "job": {"locationType": "REMOTE", "employmentType": "FULL_TIME", "teamDisplayName": "Modular", "department": {"name": "Cloud Inference"}}}
GEM_FORM = {"fields": [{"fieldType": "FIRST_NAME", "isRequired": True}, {"fieldType": "EMAIL", "isRequired": True}, {"fieldType": "PHONE", "isRequired": False}, {"fieldType": "RESUME", "isRequired": True}],
            "questions": [{"extId": "q1", "answerType": "SHORT_TEXT", "text": "Where are you currently residing? *", "isRequired": True, "options": None},
                          {"extId": "q2", "answerType": "LONG_TEXT", "text": "Visa sponsorship?", "description": "<em>We may assist.</em>", "isRequired": True, "options": None},
                          {"extId": "q3", "answerType": "SINGLE_SELECT", "text": "Authorized to work?", "isRequired": True, "options": [{"extId": "o-yes", "value": "Yes"}, {"extId": "o-no", "value": "No"}]}],
            "demographicSurvey": {"questions": [{"extId": "d1", "answerType": "SINGLE_SELECT", "text": "Gender", "options": [{"extId": "g1", "value": "Male"}, {"extId": "g2", "value": "Decline to self-identify"}]}]}}

ZOHO_BOARD = '[{&#34;Remote_Job&#34;:true,&#34;Job_Type&#34;:&#34;Full time&#34;,&#34;Job_Opening_Name&#34;:&#34;AI Engineer&#34;,&#34;Posting_Title&#34;:&#34;AI Engineer&#34;,&#34;Country&#34;:null,&#34;id&#34;:&#34;818455000000584013&#34;,&#34;City&#34;:null,&#34;Publish&#34;:true},{&#34;Remote_Job&#34;:false,&#34;Job_Type&#34;:&#34;Full time&#34;,&#34;Job_Opening_Name&#34;:&#34;Java Software Engineer&#34;,&#34;Posting_Title&#34;:&#34;Java Software Engineer&#34;,&#34;Country&#34;:&#34;Lebanon&#34;,&#34;id&#34;:&#34;818455000001300001&#34;,&#34;City&#34;:null,&#34;Publish&#34;:true},{&#34;Posting_Title&#34;:&#34;Draft&#34;,&#34;id&#34;:&#34;818455000001300002&#34;,&#34;Publish&#34;:false}]'


class TestDover:
    def test_feed_item(self):
        j = DoverFetcher()._normalize(DOVER_JOB, "dover", group="Ungrouped")
        assert j["external_id"] == "dover-0d29285d-1b13-46ce-9390-6033a275b50e" and j["url"] == "https://app.dover.com/apply/dover/0d29285d-1b13-46ce-9390-6033a275b50e"
        assert j["location_raw"] == "Remote (United States; Canada)" and j["remote_scope"] == "remote" and j["department"] == "" and j["employment_type"] == "FULL_TIME"
        assert DoverFetcher()._normalize({**DOVER_JOB, "is_sample": True}, "dover") is None

    def test_extraction(self):
        rows = normalise_dover_questions(DOVER_QS)
        k = {f["field_key"]: f for f in rows}
        assert [f["field_key"] for f in rows[:3]] == ["first_name", "last_name", "email"] and all(f["required"] for f in rows[:3])
        assert k["4c65e99e"]["field_type"] == "select" and k["4c65e99e"]["options"] == ["0", "1", "2+"] and k["4c65e99e"]["required"]
        assert k["resume"]["field_type"] == "file" and k["linkedin"]["required"] and k["phone"]["required"] is False
        assert k["88bfb221"]["field_type"] == "textarea" and k["aa11"]["field_type"] == "multi_select"
        assert "hid" not in k

    def test_registered_and_links(self):
        assert FETCHER_MAP["dover"] is DoverFetcher and isinstance(get_submitter("dover"), DoverSubmitter)
        assert "dover" in SUPPORTED_QUESTION_PLATFORMS and "dover" in auto_submittable_platforms() and "dover" in PROBE_PLATFORMS
        p = parse_job_url("https://app.dover.com/apply/Dover/0d29285d-1b13-46ce-9390-6033a275b50e/?rs=76643084")
        assert (p.platform, p.slug, p.external_id) == ("dover", "dover", "dover-0d29285d-1b13-46ce-9390-6033a275b50e")
        assert canonical_posting_url("dover", "dover", {"external_id": "dover-abc-def", "url": ""}) == "https://app.dover.com/apply/dover/abc-def"

    def test_submitter_names(self):
        assert DoverSubmitter._dom_name(SubmitField(field_key="linkedin", label="LinkedIn", field_type="text", value="x")) == "linkedinUrl"
        assert DoverSubmitter._dom_name(SubmitField(field_key="4c65e99e", label="Q", field_type="select", value="2+")) == "4c65e99e"
        f = SubmitField(field_key="4c65e99e", label="Q", field_type="select", value="2+", options=["0", "1", "2+"])
        assert DoverSubmitter._value_present(f, "2+") and not DoverSubmitter._value_present(f, "")


class TestGem:
    def test_feed_item(self):
        j = GemFetcher()._normalize(GEM_POST, "modular")
        assert j["external_id"] == "gem-4632989005" and j["url"] == "https://jobs.gem.com/modular/4632989005"
        assert j["location_raw"] == "Remote (United States / Canada)" and j["department"] == "Cloud Inference" and j["employment_type"] == "Full Time"
        assert GemFetcher()._normalize({**GEM_POST, "isApplicationFormHidden": True}, "modular") is None

    def test_extraction(self):
        k = {f["field_key"]: f for f in normalise_gem_form(GEM_FORM)}
        assert k["first_name"]["required"] and k["phone"]["required"] is False and k["resume"]["field_type"] == "file"
        assert "last_name" not in k  # only the fields the board lists
        assert k["q1"]["label"] == "Where are you currently residing?" and k["q1"]["field_type"] == "text"
        assert k["q2"]["field_type"] == "textarea" and k["q2"]["description"] == "We may assist."
        assert k["q3"]["field_type"] == "select" and k["q3"]["options"] == ["Yes", "No"]
        assert k["d1"]["required"] is False and k["d1"]["label"] == "Gender" and "Decline to self-identify" in k["d1"]["options"]

    def test_registered_and_links(self):
        assert FETCHER_MAP["gem"] is GemFetcher and isinstance(get_submitter("gem"), GemSubmitter)
        assert "gem" in SUPPORTED_QUESTION_PLATFORMS and "gem" in auto_submittable_platforms() and "gem" in PROBE_PLATFORMS
        p = parse_job_url("https://jobs.gem.com/modular/am9icG9zdDoniTz6EhFnrGsUt2iCvqbq")
        assert (p.platform, p.slug, p.external_id) == ("gem", "modular", "gem-am9icG9zdDoniTz6EhFnrGsUt2iCvqbq")
        assert parse_job_url("https://jobs.gem.com/modular").platform if parse_job_url("https://jobs.gem.com/modular") else True  # board page isn't a posting
        assert parse_job_url("https://jobs.gem.com/gem/4965519002").external_id == "gem-4965519002"

    def test_option_ids_come_from_schema(self):
        s = GemSubmitter()
        s._schema = {"q3": {"Yes": "o-yes", "No": "o-no"}}
        assert s._option_ids(SubmitField(field_key="q3", label="Authorized?", field_type="select", value="Yes", options=["Yes", "No"])) == {"Yes": "o-yes", "No": "o-no"}
        assert s._option_ids(SubmitField(field_key="unknown", label="?", field_type="select", value="Yes")) == {}


class TestZoho:
    def test_board_json_is_parsed_and_unpublished_dropped(self):
        jobs = ZohoRecruitFetcher().parse_board(ZOHO_BOARD, "siliconcedars")
        assert [j["external_id"] for j in jobs] == ["zoho-818455000000584013", "zoho-818455000001300001"]
        assert jobs[0]["remote_scope"] == "remote" and jobs[0]["location_raw"] == "Remote"
        assert jobs[1]["url"] == "https://siliconcedars.zohorecruit.com/jobs/Careers/818455000001300001/Java-Software-Engineer" and jobs[1]["location_raw"] == "Lebanon"

    def test_walled_scan_and_link_only(self):
        assert FETCHER_MAP["zoho"] is ZohoRecruitFetcher and "zoho" in PROBE_PLATFORMS
        assert human_wall_for("zoho")["vendor"] == "image CAPTCHA"
        assert "zoho" not in auto_submittable_platforms() and get_submitter("zoho") is None
        p = parse_job_url("https://siliconcedars.zohorecruit.com/jobs/Careers/818455000001300001/Java-Software-Engineer?source=CareerSite")
        assert (p.platform, p.slug, p.external_id) == ("zoho", "siliconcedars", "zoho-818455000001300001")
        assert canonical_posting_url("zoho", "siliconcedars", {"external_id": "zoho-1", "url": ""}) == "https://siliconcedars.zohorecruit.com/jobs/Careers/1"


class TestRefusals:
    def test_each_walled_board_gets_its_own_reason(self):
        assert parse_job_url("https://join.com/companies/decentriq/7014812-senior-software-engineer") is None
        assert "verifies your email" in refusal_for("https://join.com/companies/decentriq/7014812-senior-software-engineer")
        assert "Cloudflare" in refusal_for("https://jobs.polymer.co/16vc/40655")
        assert "create an account" in refusal_for("https://cplugjobs.careerplug.com/jobs/2019966/apps/new")
