"""F365 — Workable: extraction and submission, built against live boards.

Two boards (apply.workable.com/deeplight, /payabl) found via Workable's
public job search — our own seed catalogue has no Workable slugs at all.
Workable turned out to be the most automation-friendly ATS so far: open
to headless Chromium, no shadow DOM, no native <select>, and a form
that carries exactly the questions the never-infer gate exists for
(notice period, salary expectation, YES/NO screeners).

Three facts below each cost a failed live dry run:
  * ``data-ui`` is on the INPUT itself, not a wrapper. The wrapper-form
    selector matched nothing and the resume never uploaded.
  * A radio question's text lives two levels ABOVE its fieldset; the
    fieldset holds only "YES NO". Labels came back as ``QA_…`` until the
    extractor walked up.
  * Radio state commits on React's next tick. The last field placed
    read back unchecked with no settle — a race, not a failed click.

The fixture is the exact row shape ``_WORKABLE_STRUCT_JS`` returns for
deeplight/86F4823B1B, so ``normalise_workable_rows`` is tested against
what the page actually yields rather than an imagined schema.
"""

import asyncio

from app.fetchers.questions import (
    SUPPORTED_QUESTION_PLATFORMS,
    _run_in_fresh_loop,
    normalise_workable_rows,
)
from app.services.submitters import auto_submittable_platforms, get_submitter, submit_platforms
from app.services.submitters.base import SubmitField
from app.services.submitters.workable import WorkableSubmitter, _RESUME_SELECTOR, _SUBMIT_SELECTOR


def _r(tag, type_, name, dataUi="", required=False, question="", option="", value="", id_=""):
    return {"tag": tag, "type": type_, "name": name, "id": id_ or name, "dataUi": dataUi,
            "required": required, "question": question, "option": option, "value": value}


LIVE_ROWS = [
    _r("input", "text", "firstname", "firstname", True),
    _r("input", "text", "lastname", "lastname", True),
    _r("input", "email", "email", "email", True),
    _r("input", "tel", "phone", "phone", True, id_=""),
    _r("input", "text", "city", "", False),
    _r("input", "file", "", "avatar", False, id_="input_files_input_a"),
    _r("textarea", "textarea", "summary", "summary", False, question="Summary"),
    _r("input", "file", "", "resume", True, id_="input_files_input_b"),
    _r("textarea", "textarea", "cover_letter", "cover_letter", False, question="Cover letter"),
    _r("textarea", "textarea", "QA_11260385", "QA_11260385", True,
       question="Please confirm your current notice period"),
    _r("input", "radio", "QA_11260386", "option", True, question="Do you currently live in the UAE?", option="YES", value="YES"),
    _r("input", "radio", "QA_11260386", "option", True, question="Do you currently live in the UAE?", option="NO", value="NO"),
    _r("textarea", "textarea", "QA_11260387", "QA_11260387", True,
       question="Please confirm your salary expectation for this role, monthly in AED"),
]


def _by_key():
    return {f["field_key"]: f for f in normalise_workable_rows(LIVE_ROWS)}


class TestNormalisation:
    def test_fixed_fields_map_to_canonical_keys(self):
        k = _by_key()
        assert k["first_name"]["field_type"] == "text" and k["first_name"]["required"] is True
        assert k["email"]["field_type"] == "text"
        assert k["phone"]["required"] is True

    def test_resume_is_the_data_ui_resume_file_only(self):
        """Two file inputs exist; only data-ui=resume is the resume. The
        avatar must never become a field the gate asks about."""
        keys = _by_key()
        assert keys["resume"]["field_type"] == "file" and keys["resume"]["required"] is True
        assert not any(k.startswith("input_files") or k == "avatar" for k in keys)

    def test_textarea_questions_carry_real_text(self):
        k = _by_key()
        assert k["QA_11260385"]["label"] == "Please confirm your current notice period"
        assert k["QA_11260387"]["field_type"] == "textarea"

    def test_radio_group_collapses_to_one_select(self):
        k = _by_key()
        assert k["QA_11260386"]["field_type"] == "select"
        assert k["QA_11260386"]["options"] == ["YES", "NO"]
        assert k["QA_11260386"]["label"] == "Do you currently live in the UAE?"
        assert sum(1 for f in normalise_workable_rows(LIVE_ROWS) if f["field_key"] == "QA_11260386") == 1

    def test_never_infer_questions_are_visible_to_the_gate(self):
        """The reason to extract at all: the gate must see these."""
        from app.workers.tasks._answer_prep import is_never_infer_field

        k = _by_key()
        assert is_never_infer_field("QA_11260387", k["QA_11260387"]["label"])  # salary

    def test_internal_scaffolding_is_not_leaked(self):
        assert all("_names" not in f for f in normalise_workable_rows(LIVE_ROWS))


class TestWiring:
    def test_workable_is_supported_and_submittable(self):
        assert "workable" in SUPPORTED_QUESTION_PLATFORMS
        assert "workable" in submit_platforms()
        assert "workable" in auto_submittable_platforms()
        assert isinstance(get_submitter("workable"), WorkableSubmitter)

    def test_fresh_loop_runner_works_inside_a_running_loop(self):
        """The sync fetcher is called from the async endpoint; asyncio.run
        would raise there. Also asserts the pool is torn down per call."""
        async def inner():
            return 42

        async def outer():
            return _run_in_fresh_loop(inner())

        assert asyncio.run(outer()) == 42


class TestSelectors:
    def test_resume_selector_targets_the_input_itself(self):
        assert _RESUME_SELECTOR == 'input[type=file][data-ui="resume"]'

    def test_submit_is_scoped_by_data_ui_not_type(self):
        assert _SUBMIT_SELECTOR == 'button[data-ui="apply-button"]'

    def test_apply_url_is_derived_from_the_posting_url(self):
        assert WorkableSubmitter._apply_url("https://apply.workable.com/deeplight/j/86F4823B1B/") \
            == "https://apply.workable.com/deeplight/j/86F4823B1B/apply/"
        assert WorkableSubmitter._apply_url("https://apply.workable.com/x/j/Y/apply") \
            == "https://apply.workable.com/x/j/Y/apply"


class TestVerification:
    def test_radio_verified_by_label_text(self):
        f = SubmitField(field_key="QA_1", label="q", field_type="select", value="YES", options=["YES", "NO"])
        assert WorkableSubmitter._value_present(f, "YES")
        assert not WorkableSubmitter._value_present(f, "")
        assert not WorkableSubmitter._value_present(f, "NO")

    def test_phone_compared_on_digits(self):
        f = SubmitField(field_key="phone", label="Phone", field_type="text", value="+971501234567")
        assert WorkableSubmitter._value_present(f, "+971 50 123 4567")
        assert not WorkableSubmitter._value_present(f, "999")

    def test_text_needs_its_own_value_back(self):
        f = SubmitField(field_key="first_name", label="First", field_type="text", value="Probe")
        assert WorkableSubmitter._value_present(f, "Probe")
        assert not WorkableSubmitter._value_present(f, "")
