"""F388 Jobvite (scan, extract, submit) + Y Combinator own-link refusal.

Jobvite — the JSON API died in April 2026 but jobs.jobvite.com is alive
for current tenants as server-rendered HTML (progress: 31 postings, no
pagination). The apply page is AngularJS behind an optional data-consent
step (#jv-country-select + "I Accept"), surfaced as the
``jv_consent_region`` question so the user picks it, never us. reCAPTCHA
is ``size=invisible`` → not a wall. Dry run on progress placed every
field including the hidden #file-input-0 résumé.

Y Combinator — Work at a Startup applications go through the candidate's
own YC account (account.ycombinator.com), which we can't create or sign
into; a pasted link is refused with that reason instead of a generic one.
"""

from app.fetchers import FETCHER_MAP
from app.fetchers.jobvite import JobviteFetcher
from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, normalise_jobvite_rows
from app.services.own_link import parse_job_url, refusal_for
from app.services.company_lookup import PROBE_PLATFORMS, canonical_posting_url
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.base import SubmitField, detect_human_wall
from app.services.submitters.jobvite import JobviteSubmitter

BOARD = """
<h3 class="h2">Building Security</h3><table class="jv-job-list"><tbody><tr>
<td class="jv-job-list-name"><a href="/progress/job/oWtBAfwk">Principal Cybersecurity Analyst</a></td>
<td class="jv-job-list-location"> Hybrid Remote<span>,</span>   Sofia,   Bulgaria </td></tr>
<tr><td class="jv-job-list-name"><a href="/progress/job/oWtBAfwk">Dup</a></td><td class="jv-job-list-location">x</td></tr></tbody></table>
<h3 class="h2">Content Development</h3><table class="jv-job-list"><tbody><tr>
<td class="jv-job-list-name"><a href="/progress/job/oTcvAfwU">Technical Writer</a></td>
<td class="jv-job-list-location">Remote, United States</td></tr></tbody></table>
"""
DETAIL = """<html><head><title>Progress Careers - Principal Cybersecurity Analyst</title></head><body>
<h2 class="jv-header">Principal Cybersecurity Analyst</h2><p class="jv-job-detail-meta">Building Security<span class='jv-inline-separator'></span>Hybrid Remote<span>,</span> Sofia, Bulgaria</p></body></html>"""

ROWS = [
    {"kind": "consent", "label": "Location of Residence and Language:", "options": ["Any European Union (EU) Country, English", "United States of America, English", "Any Other Country, English"]},
    {"kind": "resume", "required": True},
    {"kind": "field", "tag": "input", "type": "text", "name": "input-yHhAYfwg", "id": "jv-field-yHhAYfwg", "question": "First Name*", "required": True, "options": []},
    {"kind": "field", "tag": "input", "type": "text", "name": "input-yGhAYfwf", "id": "jv-field-yGhAYfwf", "question": "Email*", "required": True, "options": []},
    {"kind": "field", "tag": "input", "type": "tel", "name": "input-yyiAYfw8", "id": "jv-field-yyiAYfw8", "question": "Phone", "required": False, "options": []},
    {"kind": "field", "tag": "input", "type": "radio", "name": "yyIyZfwx", "id": "jv-field-yyIyZfwx-0", "question": "Understanding that the amount of time a role is expected to be in an office is different, which describes you?*", "required": True,
     "options": ["Working mainly remotely", "Dividing my time between an office and working remotely", "Working mainly in an office", "I would be open to any of these options"]},
    {"kind": "field", "tag": "select", "type": "select-one", "name": "input-yHiAYfwh", "id": "jv-field-yHiAYfwh", "question": "Gender Identity*", "required": True, "options": ["Female", "Male", "Non-binary", "Decline to Self Identify"]},
    {"kind": "field", "tag": "input", "type": "checkbox", "name": "input-zz", "id": "", "question": "I agree to the privacy notice*", "required": True, "options": ["I agree to the privacy notice*"]},
]


class TestJobviteFetcher:
    def test_board_scrape_groups_by_department_and_dedupes(self):
        jobs = JobviteFetcher().parse_board(BOARD, "progress")
        assert [j["external_id"] for j in jobs] == ["jobvite-oWtBAfwk", "jobvite-oTcvAfwU"]
        assert jobs[0]["department"] == "Building Security" and jobs[0]["location_raw"] == "Hybrid Remote, Sofia, Bulgaria" and jobs[0]["remote_scope"] == "remote"
        assert jobs[1]["department"] == "Content Development" and jobs[1]["url"] == "https://jobs.jobvite.com/progress/job/oTcvAfwU"

    def test_detail_page_resolves_an_unlisted_posting(self):
        j = JobviteFetcher().parse_detail(DETAIL, "progress", "oWtBAfwk")
        assert j["title"] == "Principal Cybersecurity Analyst" and j["department"] == "Building Security"
        assert j["location_raw"] == "Hybrid Remote, Sofia, Bulgaria" and j["raw_json"]["unlisted"] is True

    def test_dead_slug_redirect_yields_nothing(self, monkeypatch):
        import httpx
        f = JobviteFetcher()
        class C:
            def get(self, url, params=None):
                return httpx.Response(200, text="<html>support</html>", request=httpx.Request("GET", "https://search.jobvite.com/?invalid=1"))
        monkeypatch.setattr(f, "_get_client", lambda: C())
        assert f.fetch("zoom") == []


class TestJobviteExtraction:
    def test_rows(self):
        k = {f["field_key"]: f for f in normalise_jobvite_rows(ROWS)}
        c = k["jv_consent_region"]
        assert c["field_type"] == "select" and c["required"] and c["label"] == "Location of Residence and Language" and len(c["options"]) == 3
        assert k["resume"]["field_type"] == "file" and k["resume"]["required"]
        assert k["first_name"]["required"] and k["email"]["field_type"] == "text" and k["phone"]["required"] is False
        r = k["yyIyZfwx"]
        assert r["field_type"] == "select" and r["label"].startswith("Understanding") and not r["label"].endswith("*") and len(r["options"]) == 4
        assert k["input-yHiAYfwh"]["label"] == "Gender Identity" and "Decline to Self Identify" in k["input-yHiAYfwh"]["options"]
        assert k["input-zz"]["field_type"] == "boolean"

    def test_registered_and_auto_submittable(self):
        assert FETCHER_MAP["jobvite"] is JobviteFetcher
        assert isinstance(get_submitter("jobvite"), JobviteSubmitter)
        assert "jobvite" in SUPPORTED_QUESTION_PLATFORMS and "jobvite" in auto_submittable_platforms()
        assert "jobvite" in PROBE_PLATFORMS

    def test_invisible_recaptcha_is_not_a_wall(self):
        html = '<iframe src="https://www.recaptcha.net/recaptcha/api2/anchor?ar=1&k=6Ldw0iMUAAAAAKozS5vJjJWj2CMgevHaMFD0uBEq&co=x&hl=en&v=B&theme=light&size=invisible&cb=1"></iframe><div class="grecaptcha-badge"></div><iframe src="https://www.recaptcha.net/recaptcha/api2/bframe?hl=en"></iframe>'
        assert detect_human_wall(html) is None  # invisible anchor + its hidden challenge frame
        assert detect_human_wall('<iframe src="https://www.google.com/recaptcha/api2/bframe?hl=en"></iframe>') is not None  # a bare challenge frame still is
        assert detect_human_wall('<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"></iframe><iframe src="https://www.google.com/recaptcha/api2/bframe"></iframe>') is not None


class TestJobviteSubmitter:
    def test_apply_url(self):
        assert JobviteSubmitter._apply_url("https://jobs.jobvite.com/progress/job/oWtBAfwk?x=1") == "https://jobs.jobvite.com/progress/job/oWtBAfwk/apply"
        assert JobviteSubmitter._apply_url("https://jobs.jobvite.com/progress/job/oWtBAfwk/apply") == "https://jobs.jobvite.com/progress/job/oWtBAfwk/apply"

    def test_select_verification(self):
        f = SubmitField(field_key="yyIyZfwx", label="Office?", field_type="select", value="Working mainly remotely", options=["Working mainly remotely", "Working mainly in an office"])
        assert JobviteSubmitter._value_present(f, "Working mainly remotely") and not JobviteSubmitter._value_present(f, "")

    def test_own_link_and_canonical(self):
        p = parse_job_url("https://jobs.jobvite.com/progress/job/oWtBAfwk")
        assert (p.platform, p.slug, p.external_id) == ("jobvite", "progress", "jobvite-oWtBAfwk")
        assert parse_job_url("https://jobs.jobvite.com/careers/progress/job/oWtBAfwk").slug == "progress"
        assert canonical_posting_url("jobvite", "progress", {"external_id": "jobvite-oWtBAfwk", "url": ""}) == "https://jobs.jobvite.com/progress/job/oWtBAfwk"


class TestYCombinatorLink:
    def test_refused_with_the_account_reason(self):
        assert parse_job_url("https://www.workatastartup.com/jobs/57417") is None
        reason = refusal_for("https://www.workatastartup.com/jobs/57417")
        assert "YC account" in reason


class TestTurnstileMarkers:
    def test_invisible_turnstile_response_input_is_not_a_wall(self):
        assert detect_human_wall('<form><input type="hidden" name="cf-turnstile-response" id="cf-chl-widget-4keia_response"><button type="submit">Apply</button></form>') is None

    def test_rendered_widget_and_cloudflare_interstitial_are_walls(self):
        assert detect_human_wall('<div class="cf-turnstile" data-sitekey="x"></div>') is not None
        assert detect_human_wall('<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/x"></iframe>') is not None
        assert detect_human_wall('<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script><input type="hidden" name="cf-turnstile-response">') is None
        assert detect_human_wall('<html><head><title>Just a moment...</title></head><body>Performing security verification</body></html>') is not None
