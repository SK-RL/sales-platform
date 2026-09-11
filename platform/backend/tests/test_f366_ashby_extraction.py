"""F366 — Ashby: extraction through the rendered application page.

F357 unwired Ashby because its posting-api application-form endpoint
returns 401 on every public board. That was the wrong probe. The
application PAGE (jobs.ashbyhq.com/{org}/{id}/application) renders fully
in headless Chromium — verified on ramp, supabase, linear and vanta — so
extraction goes through the page. Every board also carries a reCAPTCHA
v2 checkbox, so unattended submission is gated: Ashby is extraction-only
and routes to the review queue, exactly like Lever.

Fixture is the row shape ``_ASHBY_STRUCT_JS`` returned for a live Ramp
posting (Security Engineer, Cloud), trimmed not invented.
"""
from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS, normalise_ashby_rows
from app.services.submitters import auto_submittable_platforms
from app.workers.tasks._answer_prep import match_questions_to_answers


def _r(type_, name="", id_="", label="", wrap="", option="", required=False, tag="input"):
    return {"tag": tag, "type": type_, "name": name, "id": id_, "label": label,
            "wrapLabel": wrap, "option": option, "required": required}


LIVE = [
    _r("file"),  # the autofill dropzone at the top — no id, no label
    _r("text", "_systemfield_name", "_systemfield_name", "Legal Name", "Legal Name", required=True),
    _r("email", "_systemfield_email", "_systemfield_email", "Email", "Email", required=True),
    _r("tel", "4b71793a-c95c", "4b71793a-c95c", "Phone", "Phone", required=True),
    _r("radio", "communicationConsent", "", "Yes - I consent to receiving text messages", "Phone", "Yes - I consent to receiving text messages"),
    _r("radio", "communicationConsent", "", "No - I do not consent to receiving text messages", "Phone", "No - I do not consent to receiving text messages"),
    _r("text", "", "", "", "Where do you plan on working from (for payroll tax purposes)?"),
    _r("file", "", "_systemfield_resume", "Resume", "Resume", required=True),
    _r("text", "dc915b3a", "dc915b3a", "LinkedIn Profile", "LinkedIn Profile", required=True),
    _r("file", "", "6686ff1d", "Cover Letter", "Cover Letter"),
    _r("checkbox", "3080cfba", "", "", "Do you have a minimum of 7 years of experience building software?"),
    _r("textarea", "0086e069", "0086e069", "Please elaborate on your experience building software", "Please elaborate on your experience building software", required=True, tag="textarea"),
    _r("textarea", "g-recaptcha-response", "g-recaptcha-response-100000"),  # must be ignored upstream; harmless here
]


def _by_key():
    return {f["field_key"]: f for f in normalise_ashby_rows(LIVE)}


class TestNormalisation:
    def test_system_fields_map_to_canonical_keys(self):
        k = _by_key()
        assert k["name"]["label"] == "Full Name" and k["name"]["required"]
        assert k["email"]["field_type"] == "text"
        assert k["phone"]["required"] is True

    def test_resume_is_the_system_resume_only(self):
        """The top-of-page autofill dropzone is also a file input; it must
        not become a field the gate asks about."""
        k = _by_key()
        assert k["resume"]["field_type"] == "file" and k["resume"]["required"]
        assert sum(1 for f in normalise_ashby_rows(LIVE) if f["field_type"] == "file") == 2  # resume + cover letter

    def test_cover_letter_file_is_optional(self):
        assert _by_key()["cover_letter_file"]["required"] is False

    def test_custom_question_keeps_real_label(self):
        assert _by_key()["0086e069"]["label"].startswith("Please elaborate")

    def test_lone_checkbox_is_a_boolean_labelled_from_its_wrapper(self):
        k = _by_key()
        assert k["3080cfba"]["field_type"] == "boolean"
        assert k["3080cfba"]["label"].startswith("Do you have a minimum of 7 years")

    def test_consent_radios_collapse_to_one_select(self):
        k = _by_key()
        assert k["communicationConsent"]["field_type"] == "select"
        assert len(k["communicationConsent"]["options"]) == 2
        assert k["communicationConsent"]["label"] == "Communication consent (SMS)"

    def test_unnamed_location_field_is_keyed_from_its_question(self):
        k = _by_key()
        assert any(key.startswith("ashby_where_do_you_plan") for key in k)


class TestWiringAndGate:
    def test_ashby_is_extractable_but_never_auto_submitted(self):
        assert "ashby" in SUPPORTED_QUESTION_PLATFORMS
        assert "ashby" not in auto_submittable_platforms()

    def test_elaborate_is_not_a_compensation_question(self):
        """The 'rate' false positive found on this very form."""
        m = {x["field_key"]: x for x in match_questions_to_answers(normalise_ashby_rows(LIVE), [])}
        assert m["0086e069"]["never_infer"] is False
