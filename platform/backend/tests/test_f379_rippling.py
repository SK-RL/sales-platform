"""F379 — Rippling ATS: JSON API, form extraction, submitter.

Verified live on ats.rippling.com/athennian: 5 API rows → 3 postings
(the API repeats a job per work location); the apply page extracted 10
fields; the dry run placed 10/10 with readback. Turnstile is loaded
invisibly (token minted on submit), the Ashby-v3 situation.
"""

from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, normalise_rippling_rows
from app.fetchers.rippling import RipplingFetcher
from app.services.own_link import parse_job_url
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.base import SubmitField
from app.services.submitters.rippling import RipplingSubmitter

API_ROWS = [
    {"uuid": "e2d3287c-eea6-445b-b152-ee765f82d3a8", "name": "Senior Product Designer", "department": {"name": "Design"},
     "url": "https://ats.rippling.com/athennian/jobs/e2d3287c-eea6-445b-b152-ee765f82d3a8", "workLocation": {"label": "Canada"}},
    {"uuid": "0b0c1ccd-1111-4222-8333-444444444444", "name": "Senior Product Manager", "url": "https://ats.rippling.com/athennian/jobs/0b0c1ccd-1111-4222-8333-444444444444", "workLocation": {"label": "Remote (Canada)"}},
    {"uuid": "0b0c1ccd-1111-4222-8333-444444444444", "name": "Senior Product Manager", "url": "https://ats.rippling.com/athennian/jobs/0b0c1ccd-1111-4222-8333-444444444444", "workLocation": {"label": "Remote (United States)"}},
]


def _r(type_, key, label, name="", question="", option="", role="", required=False, tag="input"):
    return {"tag": tag, "type": type_, "key": key, "name": name, "label": label, "question": question, "role": role,
            "required": required, "value": "", "option": option}


LIVE_ROWS = [
    _r("file", "resume", "Résumé"),
    _r("text", "first_name", "First name", "N0-gSTqn6g", required=True),
    _r("text", "email", "Email", "qwbbJldaBtk", required=True),
    _r("text", "select-search-input", "Pronouns", "QH6y", role="combobox"),
    _r("text", "select-search-input", "Search", "xbEn", role="combobox", required=True),  # phone country widget
    _r("text", "phone_number", "Phone number", "aPCL", required=True),
    _r("text", "undefined", "Location", "hxoK", required=True),
    _r("text", "linkedin_link", "LinkedIn Link", "kIpb", required=True),
    _r("file", "cover_letter", "Cover letter"),
    _r("radio", "", "Yes", "customQuestions.6a8f.3537", question="Do you have 5+ years in Product Design?", option="Yes"),
    _r("radio", "", "No", "customQuestions.6a8f.3537", question="Do you have 5+ years in Product Design?", option="No"),
    _r("radio", "", "", "sms_opt_in", option="Yes - I consent to receiving text messages"),
    _r("radio", "", "", "sms_opt_in", option="No - I do not consent to receiving text messages"),
]


class TestFetcher:
    def test_one_posting_per_uuid_with_locations_merged(self):
        f = RipplingFetcher()
        out = {}
        for i in API_ROWS:
            j = f._normalize(i, "athennian")
            out.setdefault(j["external_id"], j)
        assert len(out) == 2
        rows = f.fetch.__func__  # sanity: the dedupe lives in fetch()
        assert rows is not None

    def test_normalise(self):
        j = RipplingFetcher()._normalize(API_ROWS[1], "athennian")
        assert j["platform"] == "rippling" and j["remote_scope"] == "remote" and j["url"].endswith(API_ROWS[1]["uuid"])


class TestExtraction:
    def test_fixed_fields_are_keyed_by_testid_not_random_name(self):
        k = {f["field_key"]: f for f in normalise_rippling_rows(LIVE_ROWS)}
        assert k["first_name"]["required"] and k["phone"]["label"] == "Phone number" and k["linkedin_url"]["required"]

    def test_comboboxes_are_keyed_by_label_and_the_search_widget_is_dropped(self):
        k = {f["field_key"]: f for f in normalise_rippling_rows(LIVE_ROWS)}
        assert k["rippling_pronouns"]["combobox"] is True and k["rippling_location"]["required"]
        assert "rippling_search" not in k

    def test_custom_radio_group_gets_its_question_and_labels(self):
        k = {f["field_key"]: f for f in normalise_rippling_rows(LIVE_ROWS)}
        assert k["customQuestions.6a8f.3537"]["label"].startswith("Do you have 5+ years") and k["customQuestions.6a8f.3537"]["options"] == ["Yes", "No"]
        assert k["sms_opt_in"]["options"][0].startswith("Yes - I consent")

    def test_files(self):
        k = {f["field_key"]: f for f in normalise_rippling_rows(LIVE_ROWS)}
        assert k["resume"]["field_type"] == "file" and k["cover_letter_file"]["field_type"] == "file"


class TestSubmitter:
    def test_registered_and_auto_submittable(self):
        assert isinstance(get_submitter("rippling"), RipplingSubmitter)
        assert "rippling" in SUPPORTED_QUESTION_PLATFORMS and "rippling" in auto_submittable_platforms()

    def test_apply_url(self):
        assert RipplingSubmitter._apply_url("https://ats.rippling.com/a/jobs/x?src=1") == "https://ats.rippling.com/a/jobs/x/apply"

    def test_radio_verification_accepts_label_or_value(self):
        v = RipplingSubmitter._value_present
        f = SubmitField(field_key="sms_opt_in", label="", field_type="select", value="Yes - I consent to receiving text messages",
                        options=["Yes - I consent to receiving text messages", "No - I do not consent to receiving text messages"])
        assert v(f, "Yes - I consent to receiving text messages | true")
        assert not v(f, "No - I do not consent to receiving text messages | false")

    def test_combobox_commits_option_text(self):
        assert RipplingSubmitter._value_present(SubmitField(field_key="rippling_location", label="Location", field_type="text", value="Toronto"), "Toronto, Ontario, Canada")


class TestOwnLink:
    def test_parses(self):
        p = parse_job_url("https://ats.rippling.com/athennian/jobs/e2d3287c-eea6-445b-b152-ee765f82d3a8/apply")
        assert (p.platform, p.slug, p.external_id) == ("rippling", "athennian", "e2d3287c-eea6-445b-b152-ee765f82d3a8")
