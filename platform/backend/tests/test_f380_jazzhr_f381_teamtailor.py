"""F380 JazzHR (scan + extraction, walled) and F381 Teamtailor (scan, extract, submit).

JazzHR — verified live on lumivero.applytojob.com: no feed exists, the
board page is scraped (6 postings); the apply page extracted 16 fields;
every posting carries a reCAPTCHA v2 checkbox → KNOWN_HUMAN_WALLS,
never auto-submitted.

Teamtailor — verified live on virtasant / clearroute / xci: RSS feed
(20 jobs on virtasant), Rails form at /jobs/{id}/applications/new with
no captcha; 10 fields extracted; dry run placed 10/10. Rails emits a
hidden <input name=…> twin before each checkbox, so booleans are
addressed as input[type=checkbox].
"""

from app.fetchers.jazzhr import JazzHRFetcher
from app.fetchers.questions import (
    SUPPORTED_QUESTION_PLATFORMS,
    human_wall_for,
    normalise_teamtailor_rows,
    parse_jazzhr_form,
)
from app.fetchers.teamtailor import TeamtailorFetcher
from app.services.own_link import parse_job_url
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.base import SubmitField
from app.services.submitters.teamtailor import TeamtailorSubmitter

JAZZ_BOARD = """
<ul class="list-group"><li class="list-group-item"><h3 class='list-group-item-heading'>
<a href="https://lumivero.applytojob.com/apply/MSVT14vftm/Devops-Engineer">Devops Engineer</a></h3>
<ul class='list-inline list-group-item-text'><li><i class='fa fa-map-marker'></i>Remote</li><li>Engineering</li></ul></li>
<li class="list-group-item"><h3><a href="https://lumivero.applytojob.com/apply/MSVT14vftm/Devops-Engineer">Devops Engineer (dup)</a></h3></li></ul>
"""
JAZZ_FORM = """
<form><label for="resumator-firstname-value">First Name*</label><input id="resumator-firstname-value" name="resumator-firstname-value">
<label for="resumator-email-value">Email Address*</label><input type="email" id="resumator-email-value" name="resumator-email-value">
<label for="resumator-resume-value">Resume*</label><input type="file" id="resumator-resume-value" name="resumator-resume-value">
<label for="resumator-questionnaire-q1">Are you an AI?*</label><select id="resumator-questionnaire-q1" name="resumator-questionnaire[1]"><option>Please select</option><option>Yes</option><option>No</option></select>
<label for="resumator-questionnaire-q2">Which Cloud Provider?</label><input id="resumator-questionnaire-q2" name="resumator-questionnaire[2]">
<label class="control-label">Are you authorized to work in Mexico?*</label>
<input type="checkbox" id="resumator-checkbox-9-1" name="resumator-checkbox-9-1"><label for="resumator-checkbox-9-1">Yes</label>
<input type="checkbox" id="resumator-checkbox-9-2" name="resumator-checkbox-9-2"><label for="resumator-checkbox-9-2">No</label>
<textarea name="g-recaptcha-response"></textarea></form>
"""


class TestJazzHR:
    def test_board_scrape_dedupes_by_code(self):
        jobs = JazzHRFetcher().parse_board(JAZZ_BOARD, "lumivero")
        assert len(jobs) == 1 and jobs[0]["external_id"] == "jazzhr-MSVT14vftm" and jobs[0]["remote_scope"] == "remote"
        assert jobs[0]["department"] == "Engineering"

    def test_form_extraction(self):
        k = {f["field_key"]: f for f in parse_jazzhr_form(JAZZ_FORM)}
        assert k["first_name"]["required"] and k["email"]["field_type"] == "text" and k["resume"]["field_type"] == "file"
        assert k["resumator-questionnaire[1]"]["options"] == ["Yes", "No"]
        assert k["resumator-questionnaire[2]"]["required"] is False
        assert k["resumator-checkbox-9"]["field_type"] == "multi_select" and k["resumator-checkbox-9"]["options"] == ["Yes", "No"]
        assert k["resumator-checkbox-9"]["label"].startswith("Are you authorized")
        assert "g-recaptcha-response" not in k

    def test_unlisted_posting_resolves_from_its_own_page(self, monkeypatch):
        """lumivero's DevOps posting answers 200 but the board omits it."""
        f = JazzHRFetcher()
        monkeypatch.setattr(f, "fetch", lambda slug: [])
        class R:
            status_code = 200; url = "https://lumivero.applytojob.com/apply/MSVT14vftm/"
            text = "<html><head><title>Devops Engineer - Career Page</title></head><body></body></html>"
        class C:
            def get(self, url): return R()
        monkeypatch.setattr(f, "_get_client", lambda: C())
        j = f.fetch_one("lumivero", "jazzhr-MSVT14vftm")
        assert j["title"] == "Devops Engineer" and j["external_id"] == "jazzhr-MSVT14vftm" and j["raw_json"]["unlisted"] is True

    def test_walled_and_extract_only(self):
        assert "jazzhr" in SUPPORTED_QUESTION_PLATFORMS
        assert human_wall_for("jazzhr")["vendor"] == "reCAPTCHA"
        assert "jazzhr" not in auto_submittable_platforms()

    def test_own_link(self):
        p = parse_job_url("https://lumivero.applytojob.com/apply/MSVT14vftm/Devops-Engineer")
        assert (p.platform, p.slug, p.external_id) == ("jazzhr", "lumivero", "jazzhr-MSVT14vftm")


TT_ITEM = """<title>Build &amp; Release Support Engineer | CI/CD</title><link>https://virtasant.teamtailor.com/jobs/8362291-build-release</link>
<description>&lt;p&gt;Location: Remote&lt;/p&gt;</description><pubDate>Thu, 11 Sep 2026 10:00:00 +0000</pubDate>
<tt:department>Engineering</tt:department><tt:location>Remote</tt:location><remoteStatus>fully_remote</remoteStatus>"""


def _r(tag, type_, name, id_="", label="", legend="", required=False, options=None):
    return {"tag": tag, "type": type_, "name": name, "id": id_, "label": label, "legend": legend,
            "required": required, "min": "", "max": "", "options": options or []}


TT_ROWS = [
    _r("input", "radio", "candidate[answers_attributes][0][choice]", "c0_1", "APAC", "In what region will you be joining us?* Required", True),
    _r("input", "radio", "candidate[answers_attributes][0][choice]", "c0_2", "EMEA", "In what region will you be joining us?* Required", True),
    _r("input", "text", "candidate[answers_attributes][2][text]", "a2", "Are you currently on a visa or require any sponsorship?* Required", "", True),
    _r("input", "checkbox", "candidate[location_ids][]", "loc1", "USA", "Locations* Required", True),
    _r("input", "text", "candidate[first_name]", "candidate_first_name", "First name* Required", "", True),
    _r("input", "email", "candidate[email]", "candidate_email", "Email* Required", "", True),
    _r("input", "file", "", "candidate_resume_remote_url", "Upload CV* Required", "", True),
    _r("input", "checkbox", "candidate[consent_given]", "candidate_consent_given", "Required. By submitting this application, I agree…", "", True),
]


class TestTeamtailor:
    def test_feed_item(self):
        j = TeamtailorFetcher()._normalize(TT_ITEM, "virtasant")
        assert j["external_id"] == "teamtailor-8362291" and j["remote_scope"] == "remote" and j["department"] == "Engineering"
        assert j["title"].startswith("Build & Release")

    def test_extraction(self):
        k = {f["field_key"]: f for f in normalise_teamtailor_rows(TT_ROWS)}
        assert k["candidate[answers_attributes][0][choice]"]["options"] == ["APAC", "EMEA"]
        assert k["candidate[answers_attributes][0][choice]"]["label"].startswith("In what region")
        assert k["candidate[location_ids][]"]["field_type"] == "multi_select" and k["candidate[location_ids][]"]["label"] == "Locations"
        assert k["first_name"]["required"] and k["resume"]["field_type"] == "file"
        assert k["privacy_consent"]["field_type"] == "boolean" and k["privacy_consent"]["required"] is True
        assert k["candidate[answers_attributes][2][text]"]["label"].endswith("sponsorship?")

    def test_registered_and_auto_submittable(self):
        assert isinstance(get_submitter("teamtailor"), TeamtailorSubmitter)
        assert "teamtailor" in SUPPORTED_QUESTION_PLATFORMS and "teamtailor" in auto_submittable_platforms()

    def test_boolean_is_addressed_as_the_checkbox_not_rails_hidden_twin(self):
        assert TeamtailorSubmitter._sel("privacy_consent", "boolean") == '#job-application-form input[type=checkbox][name="candidate[consent_given]"]'

    def test_apply_url(self):
        assert TeamtailorSubmitter._apply_url("https://x.teamtailor.com/jobs/1-a?src=1") == "https://x.teamtailor.com/jobs/1-a/applications/new"

    def test_multi_select_verification(self):
        f = SubmitField(field_key="candidate[location_ids][]", label="Locations", field_type="multi_select", value="USA", options=["USA", "Latin America"])
        assert TeamtailorSubmitter._value_present(f, "USA") and not TeamtailorSubmitter._value_present(f, "")

    def test_own_link_including_regional_hosts(self):
        assert parse_job_url("https://virtasant.teamtailor.com/jobs/8237846-lead").external_id == "teamtailor-8237846"
        assert parse_job_url("https://ioet.na.teamtailor.com/jobs/685145-site-reliability-engineer").slug == "ioet"


class TestPasteTimeBudget:
    def test_probe_runs_platforms_in_parallel_with_a_short_timeout(self):
        import inspect
        from app.services import company_lookup as cl
        assert cl.PROBE_TIMEOUT_S <= 15 and "ThreadPoolExecutor" in inspect.getsource(cl.probe_boards)

    def test_himalayas_page_is_skipped_by_default(self, monkeypatch):
        import app.services.aggregator_resolver as ar
        called = {"page": 0}
        def page(url):
            called["page"] += 1
            return ar.Resolution("blocked")
        monkeypatch.setattr(ar, "resolve_page", page)
        monkeypatch.setattr("app.services.company_lookup.lookup", lambda s, j: None)
        class Row:  # minimal job
            platform = "himalayas"; url = "https://himalayas.app/x"; company_id = None; raw_json = {}; title = "SRE"; id = "j"
            apply_url = apply_platform = resolved_job_id = apply_resolve_status = apply_resolved_at = None
        class S:
            def commit(self): pass
        out = ar.resolve_job(S(), Row())
        assert called["page"] == 0 and out["status"] == "unmatched"

    def test_whole_paste_flow_is_under_one_deadline(self):
        import inspect
        from app.api.v1 import applications
        src = inspect.getsource(applications.application_from_url)
        assert "asyncio.wait_for(asyncio.to_thread(_whole_flow)" in src and applications.REPOST_RESOLVE_BUDGET_S < 60


class TestFetchOneToleratesBothIdForms:
    """Rows created before F383 carry raw ids; the extractor's fetch_one
    must find the posting with either form (production: a Teamtailor
    job extracted as the fallback template)."""

    def test_raw_and_namespaced(self, monkeypatch):
        f = TeamtailorFetcher()
        monkeypatch.setattr(f, "fetch", lambda slug: [{"external_id": "teamtailor-8237846", "title": "FDE", "url": "u"}])
        assert f.fetch_one("virtasant", "8237846")["title"] == "FDE"
        assert f.fetch_one("virtasant", "teamtailor-8237846")["title"] == "FDE"
        assert f.fetch_one("virtasant", "999") is None
