"""F378 — Personio: XML feed, form extraction, submitter.

Verified live on greenbone-ag.jobs.personio.com (2 positions; the .de
tenant maibornwolff has 47; an unknown tenant is empty on both hosts).
The apply page extracted 11 fields; the dry run placed 10/11 (the 11th
is the optional cover-letter file). Required is the "* (required)" label
suffix, not the attribute; the page is requested with ?language=en.
"""

from app.fetchers.personio import PersonioFetcher
from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, _clean_personio_label, normalise_personio_rows
from app.services.own_link import parse_job_url
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.base import SubmitField
from app.services.submitters.personio import PersonioSubmitter
from app.workers.tasks._answer_prep import match_questions_to_answers

XML_BLOCK = """<id>2546372</id><subcompany>Greenbone AG</subcompany><office>Homeoffice</office><department>Sales</department>
<recruitingCategory>Festangestellte</recruitingCategory><name>Account Manager (m/w/d) - DACH - 100% remote</name>
<jobDescriptions></jobDescriptions><employmentType>permanent</employmentType><seniority>experienced</seniority>
<schedule>full-time</schedule><createdAt>2026-03-23T10:00:00+01:00</createdAt>"""


def _r(tag, type_, name, label, options=None):
    return {"tag": tag, "type": type_, "name": name, "id": f"field-{name}", "label": label,
            "required": "(required)" in label or "*" in label, "options": options or []}


LIVE_ROWS = [
    _r("input", "text", "first_name", "First"),
    _r("input", "text", "last_name", "Last"),
    _r("input", "email", "email", "Email* (required)"),
    _r("input", "text", "phone", "Phone* (required)"),
    _r("input", "text", "custom_attribute_3960215", "What is your notice period?* (required)"),
    _r("input", "text", "salary_expectations", "Expected salary* (required)"),
    _r("select", "select-one", "custom_attribute_3845917", "Do you currently live in Germany?* (required)", ["Yes", "No"]),
    _r("input", "file", "documents.cv", "Upload CV"),
    _r("input", "file", "documents.cover-letter", "Upload Cover letter"),
    _r("input", "file", "documents.other", "Upload Other"),
]


class TestFetcher:
    def test_normalises_a_position(self):
        j = PersonioFetcher()._normalize(XML_BLOCK, "greenbone-ag", "https://greenbone-ag.jobs.personio.com")
        assert j["external_id"] == "2546372" and j["url"] == "https://greenbone-ag.jobs.personio.com/job/2546372"
        assert j["remote_scope"] == "remote" and j["department"] == "Sales" and j["raw_json"]["company_name"] == "Greenbone AG"

    def test_registered(self):
        from app.fetchers import FETCHER_MAP
        assert FETCHER_MAP["personio"] is PersonioFetcher


class TestExtraction:
    def test_labels_lose_the_required_suffix_and_required_is_read_from_it(self):
        k = {f["field_key"]: f for f in normalise_personio_rows(LIVE_ROWS)}
        assert k["email"]["label"] == "Email" and k["email"]["required"] is True
        assert k["first_name"]["label"] == "First Name" and k["first_name"]["required"] is False
        assert _clean_personio_label("Telefon* (erforderlich)") == "Telefon"

    def test_documents_map_to_resume_and_cover_letter_and_other_is_dropped(self):
        k = {f["field_key"]: f for f in normalise_personio_rows(LIVE_ROWS)}
        assert k["resume"]["field_type"] == "file" and k["cover_letter_file"]["field_type"] == "file"
        assert "documents.other" not in k

    def test_custom_select_keeps_options_without_placeholder(self):
        k = {f["field_key"]: f for f in normalise_personio_rows(LIVE_ROWS)}
        assert k["custom_attribute_3845917"]["options"] == ["Yes", "No"]

    def test_salary_is_never_inferred(self):
        m = {q["field_key"]: q for q in match_questions_to_answers(normalise_personio_rows(LIVE_ROWS), [])}
        assert m["salary_expectations"]["never_infer"] is True
        assert m["custom_attribute_3960215"]["never_infer"] is False


class TestSubmitter:
    def test_registered_and_auto_submittable(self):
        assert isinstance(get_submitter("personio"), PersonioSubmitter)
        assert "personio" in SUPPORTED_QUESTION_PLATFORMS and "personio" in auto_submittable_platforms()

    def test_apply_url_forces_english(self):
        assert PersonioSubmitter._apply_url("https://x.jobs.personio.com/job/1") == "https://x.jobs.personio.com/job/1/apply?language=en"
        assert PersonioSubmitter._apply_url("https://x.jobs.personio.de/job/1/apply?language=de") == "https://x.jobs.personio.de/job/1/apply?language=en"

    def test_value_checks(self):
        v = PersonioSubmitter._value_present
        assert v(SubmitField(field_key="custom_attribute_1", label="", field_type="select", value="Yes", options=["Yes", "No"]), "Yes")
        assert not v(SubmitField(field_key="custom_attribute_1", label="", field_type="select", value="Yes", options=["Yes", "No"]), "No")
        assert v(SubmitField(field_key="phone", label="", field_type="text", value="+49 30 1234567"), "+49301234567")


class TestOwnLink:
    def test_both_hosts_parse(self):
        p = parse_job_url("https://greenbone-ag.jobs.personio.com/job/2546372?language=en")
        assert (p.platform, p.slug, p.external_id) == ("personio", "greenbone-ag", "2546372")
        assert parse_job_url("https://maibornwolff.jobs.personio.de/job/1195240/apply").slug == "maibornwolff"
