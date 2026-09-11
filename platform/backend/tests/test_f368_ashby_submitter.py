"""F368 — Ashby submitter, and the wall map that explains the rest.

Built against the live Ramp posting the extractor (F366) reads. The
live dry run placed 13 of 14 fields with readback verification; the
14th is the optional cover-letter *file*, which we never synthesise.

What is pinned here is the logic the live form forced:

1. **Ashby's reCAPTCHA is v3, invisible.** The F366 classification
   (v2 checkbox → extraction-only) came from the bare ``api2/anchor``
   marker; the real anchor says ``size=invisible``. See
   test_f347::TestHumanWallDetection for the detector.
2. **One wrapper can hold two questions.** The Phone entry contains the
   tel input AND the SMS-consent radios. A wrapper-level stamp let the
   second overwrite the first — phone read back as the consent label
   and was demoted as unverified. Text fields are stamped on the
   control, choice groups on the wrapper.
3. **Yes/No are buttons with aria-pressed**, backed by a hidden checkbox
   that only reflects "Yes". Readback reads the button.
4. **The location combobox** offers ``[role=option]`` results; we take
   one only when every word of the answer is in it, shortest wins.
5. **Upload File and Yes/No are ``type=submit`` too**, so the submit
   control is addressed by its text.

And the platform-level counterpart: ``KNOWN_HUMAN_WALLS`` tells the
review queue *why* SmartRecruiters, BambooHR and Lever need a person.
"""

import pytest

from app.fetchers.questions import (
    KNOWN_HUMAN_WALLS,
    SUPPORTED_QUESTION_PLATFORMS,
    human_wall_for,
)
from app.services.submitters import auto_submittable_platforms, get_submitter
from app.services.submitters.ashby import (
    _CONFIRMATION_MARKERS,
    _SUBMIT_SELECTOR,
    AshbySubmitter,
    choose_combobox_option,
    resolve_control,
    resolve_entry,
)
from app.services.submitters.base import SubmitField


def _f(key, label="", ftype="text", value="x", options=None, required=False):
    return SubmitField(field_key=key, label=label or key, field_type=ftype, value=value,
                       options=options or [], required=required)


def _e(idx, label, ctrls):
    """A wrapper as _ENTRIES_JS reports it. ctrls: (id, name, type[, combobox])."""
    rows = [{"ci": i, "id": c[0], "name": c[1], "type": c[2], "combobox": len(c) > 3 and c[3]}
            for i, c in enumerate(ctrls)]
    return {"idx": idx, "label": label, "ids": [c["id"] for c in rows if c["id"]],
            "names": [c["name"] for c in rows if c["name"]], "types": [c["type"] for c in rows],
            "ctrls": rows, "combobox": any(c["combobox"] for c in rows),
            "yesno": label.startswith("Do you have")}


# The live Ramp form, as the entries script reports it.
ENTRIES = [
    _e(0, "Legal Name", [("_systemfield_name", "_systemfield_name", "text")]),
    _e(1, "Email", [("_systemfield_email", "_systemfield_email", "email")]),
    _e(2, "Phone", [("4b71793a", "4b71793a", "tel"), ("", "communicationConsent", "radio"), ("", "communicationConsent", "radio")]),
    _e(3, "Where do you plan on working from (for payroll tax purposes)?", [("", "", "text", True)]),
    _e(4, "Resume", [("_systemfield_resume", "", "file")]),
    _e(5, "LinkedIn Profile", [("dc915b3a", "dc915b3a", "text")]),
    _e(6, "Cover Letter", [("6686ff1d", "", "file")]),
    _e(7, "Do you have a minimum of 7 years of experience building software?", [("", "3080cfba", "checkbox")]),
    _e(8, "Please elaborate on your experience", [("0086e069", "0086e069", "textarea")]),
]


class TestRegistry:
    def test_ashby_is_registered_and_auto_submittable(self):
        assert isinstance(get_submitter("ashby"), AshbySubmitter)
        assert "ashby" in auto_submittable_platforms()

    def test_submit_is_addressed_by_text_not_type(self):
        """Upload File and the Yes/No options are type=submit as well."""
        assert "Submit Application" in _SUBMIT_SELECTOR
        assert _SUBMIT_SELECTOR != "button[type=submit]"

    def test_confirmation_copy_is_ashbys_own(self):
        assert "your application was successfully submitted" in _CONFIRMATION_MARKERS


class TestApplyUrl:
    def test_posting_url_gets_the_application_suffix(self):
        assert AshbySubmitter._apply_url("https://jobs.ashbyhq.com/ramp/abc") == \
            "https://jobs.ashbyhq.com/ramp/abc/application"

    def test_already_an_application_url(self):
        u = "https://jobs.ashbyhq.com/ramp/abc/application"
        assert AshbySubmitter._apply_url(u) == u

    def test_query_string_and_trailing_slash_are_dropped(self):
        assert AshbySubmitter._apply_url("https://jobs.ashbyhq.com/ramp/abc/?utm=x") == \
            "https://jobs.ashbyhq.com/ramp/abc/application"


class TestEntryResolution:
    def test_system_fields_by_stable_id(self):
        assert resolve_entry(_f("name"), ENTRIES) == 0
        assert resolve_entry(_f("email"), ENTRIES) == 1
        assert resolve_entry(_f("resume", ftype="file"), ENTRIES) == 4

    def test_phone_by_tel_control(self):
        assert resolve_entry(_f("phone"), ENTRIES) == 2

    def test_uuid_questions_by_id_or_name(self):
        assert resolve_entry(_f("dc915b3a"), ENTRIES) == 5
        assert resolve_entry(_f("3080cfba", ftype="boolean"), ENTRIES) == 7
        assert resolve_entry(_f("communicationConsent", ftype="select"), ENTRIES) == 2

    def test_unnamed_location_field_by_the_extractors_slug(self):
        key = "ashby_where_do_you_plan_on_working_from_for_payroll_tax_purposes"
        assert resolve_entry(_f(key), ENTRIES) == 3

    def test_cover_letter_file(self):
        assert resolve_entry(_f("cover_letter_file", ftype="file"), ENTRIES) == 6

    def test_unknown_key_with_unknown_label_is_none(self):
        assert resolve_entry(_f("nope", label="Not on this form"), ENTRIES) is None

    def test_duplicate_question_text_fails_safe(self):
        dup = ENTRIES + [_e(9, "LinkedIn Profile", [("zz", "zz", "text")])]
        assert resolve_entry(_f("ashby_linkedin_profile"), dup) is None


class TestControlResolution:
    """Live defect: phone shares its wrapper with the consent radios."""

    def test_phone_picks_the_tel_input_not_the_radios(self):
        assert resolve_control(_f("phone"), ENTRIES[2]) == 0

    def test_system_name(self):
        assert resolve_control(_f("name"), ENTRIES[0]) == 0

    def test_textarea_by_id(self):
        assert resolve_control(_f("0086e069"), ENTRIES[8]) == 0

    def test_file_only_wrapper_has_no_text_control(self):
        assert resolve_control(_f("resume"), ENTRIES[4]) is None


class TestComboboxChoice:
    OPTIONS = [
        "New York City, New York, United States",
        "New York, United States",
        "New York metropolitan area, United States",
        "New York Mills, Minnesota, United States",
    ]

    def test_exact_text_wins(self):
        assert choose_combobox_option("New York, United States", self.OPTIONS) == "New York, United States"

    def test_all_words_must_appear_shortest_wins(self):
        assert choose_combobox_option("New York", self.OPTIONS) == "New York, United States"

    def test_no_containing_option_means_no_choice(self):
        """A result that merely comes first is not the user's answer."""
        assert choose_combobox_option("Bengaluru, India", self.OPTIONS) is None

    def test_empty_answer_is_none(self):
        assert choose_combobox_option("", self.OPTIONS) is None


def _v(f, got):
    return AshbySubmitter._value_present(f, got)


class TestValueVerification:

    def test_boolean_reads_the_pressed_button(self):
        assert _v(_f("k", ftype="boolean", value="true"), "Yes")
        assert not _v(_f("k", ftype="boolean", value="Yes"), "No")
        assert not _v(_f("k", ftype="boolean", value="Yes"), "")

    def test_choice_matches_option_text(self):
        opts = ["Yes - I consent to receiving text messages", "No - I do not consent"]
        assert _v(_f("k", ftype="select", value=opts[0], options=opts), opts[0])
        assert not _v(_f("k", ftype="select", value=opts[0], options=opts), opts[1])

    def test_phone_compared_on_digits(self):
        assert _v(_f("phone", value="+1 415 555 0100"), "14155550100")
        assert not _v(_f("phone", value="+1 415 555 0100"), "14155550199")

    def test_combobox_commits_the_option_text_not_the_typed_value(self):
        assert _v(_f("loc", value="New York"), "New York, United States")

    def test_text_prefix(self):
        assert _v(_f("t", value="Dry-run text answer"), "Dry-run text answer generated")
        assert not _v(_f("t", value="Dry-run text answer"), "")


class TestKnownWalls:
    @pytest.mark.parametrize("platform", ["lever", "bamboohr", "smartrecruiters"])
    def test_walled_platforms_have_a_reason_and_no_submitter(self, platform):
        wall = human_wall_for(platform)
        assert wall and wall["vendor"] and wall["reason"]
        assert platform not in auto_submittable_platforms()

    def test_drivable_platforms_have_no_wall(self):
        for platform in auto_submittable_platforms():
            assert human_wall_for(platform) is None, platform

    def test_wall_and_submitter_sets_are_disjoint(self):
        """Either we can drive it or we can explain why not — never both."""
        assert not (set(KNOWN_HUMAN_WALLS) & auto_submittable_platforms())

    def test_lookup_is_case_insensitive(self):
        assert human_wall_for(" SmartRecruiters ") is not None
        assert human_wall_for("workday") is None

    def test_smartrecruiters_reason_names_datadome(self):
        assert "DataDome" in human_wall_for("smartrecruiters")["reason"]

    def test_gate_reports_the_wall_reason(self, monkeypatch):
        """apply_task's Gate 2 must say 'hCaptcha', not 'no submitter'."""
        import inspect
        import app.workers.tasks.apply_task as at
        src = inspect.getsource(at.submit_application_task)
        assert "human_wall_for(job.platform)" in src

    def test_preview_carries_the_wall(self):
        import inspect
        from app.api.v1 import applications
        assert '"wall": human_wall_for(job.platform)' in inspect.getsource(applications.preview_job_questions)

    def test_preview_never_calls_a_walled_form_safe(self):
        """Seen on production: a Lever application with 10 extracted
        fields and no blockers reported safe_to_auto_submit=True."""
        import inspect
        from app.api.v1 import applications
        src = inspect.getsource(applications.preview_job_questions)
        i = src.find('"safe_to_auto_submit"')
        assert i > 0
        clause = src[i : i + 400]
        assert "human_wall_for(job.platform) is None" in clause
        assert "job.platform in auto_submittable_platforms()" in clause
