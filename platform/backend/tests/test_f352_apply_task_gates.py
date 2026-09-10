"""F352 — apply_task's gates, actually executed.

F347 built the orchestration and F349 tested its *constants*, but the
function itself had never run: every gate, every status transition and
the ApplicationSubmission write were unexercised. This drives the task
end to end against a fake session so each refusal path is proven rather
than assumed.

The suite is deliberately DB-free (see conftest — there are no database
fixtures), so the session is faked by dispatching on the entity in each
``select()``. That keeps these tests in the same millisecond-scale tier
as the rest of the suite while still executing the real code path.
"""

import uuid

import pytest

import app.services.question_service as qsvc
import app.services.submitters as submitters
import app.workers.tasks.apply_task as at
from app.services.submitters import BaseSubmitter, SubmitOutcome


# ── fakes ──────────────────────────────────────────────────────────

class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Serves rows by the entity being selected."""

    def __init__(self, rows: dict):
        self.rows = rows
        self.added = []
        self.commits = 0
        self.closed = False

    def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        return _Result(self.rows.get(entity.__name__, []))

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FakeSubmitter(BaseSubmitter):
    platform = "greenhouse"
    outcome = SubmitOutcome(status="submitted", confirmation_text="thank you for applying")
    calls: list = []

    async def submit(self, **kw):
        FakeSubmitter.calls.append(kw)
        return FakeSubmitter.outcome


APP_ID = uuid.uuid4()


def _rows(status="prepared", platform="greenhouse", resume_bytes=b"%PDF-1.4"):
    from app.models.application import Application
    from app.models.company import CompanyATSBoard
    from app.models.job import Job
    from app.models.resume import Resume

    return {
        Application.__name__: [Row(
            id=APP_ID, user_id=uuid.uuid4(), job_id=uuid.uuid4(),
            resume_id=uuid.uuid4(), status=status, platform_response=None,
            submitted_at=None, apply_method="", submission_source="",
        )],
        Job.__name__: [Row(
            id=uuid.uuid4(), company_id=uuid.uuid4(), platform=platform,
            external_id="123", url="https://boards.greenhouse.io/acme/jobs/1",
        )],
        CompanyATSBoard.__name__: [Row(slug="acme")],
        "AnswerBookEntry": [Row(
            question_key="first_name", answer="Sarthak",
            category="personal_info", source="manual", resume_id=None,
        )],
        Resume.__name__: [Row(id=uuid.uuid4(), file_data=resume_bytes, file_type="pdf")],
    }


QUESTIONS_OK = [{
    "field_key": "first_name", "label": "First Name", "field_type": "text",
    "required": True, "options": [], "description": "",
    "extraction_mode": "extracted", "alternative_group": "",
}]


@pytest.fixture
def wired(monkeypatch):
    """Install fakes; return a handle to the session that gets used."""
    state = {}

    def _session():
        state["session"] = FakeSession(state["rows"])
        return state["session"]

    monkeypatch.setattr(at, "SyncSession", _session)
    monkeypatch.setattr(qsvc, "get_or_fetch_questions_sync",
                        lambda s, j, slug: state["questions"])
    monkeypatch.setattr(submitters, "get_submitter", lambda p: FakeSubmitter())
    FakeSubmitter.calls = []
    FakeSubmitter.outcome = SubmitOutcome(
        status="submitted", confirmation_text="thank you for applying")
    state["rows"] = _rows()
    state["questions"] = list(QUESTIONS_OK)
    return state


def run(app_id=APP_ID, **kw):
    return at.submit_application_task(str(app_id), **kw)


# ── the gates ──────────────────────────────────────────────────────

class TestGateNoAdapter:
    def test_unsupported_platform_goes_to_needs_user(self, wired):
        wired["rows"] = _rows(platform="workday")
        out = run()
        assert out["status"] == at.STATUS_NEEDS_USER
        assert "workday" in out["reason"]

    def test_it_never_reaches_the_submitter(self, wired):
        wired["rows"] = _rows(platform="workday")
        run()
        assert FakeSubmitter.calls == []


class TestGateGuessedSchema:
    def test_fallback_schema_goes_to_needs_user(self, wired):
        """A form we invented cannot be a form we filled correctly."""
        wired["questions"] = [{**QUESTIONS_OK[0], "extraction_mode": "fallback"}]
        out = run()
        assert out["status"] == at.STATUS_NEEDS_USER
        assert "real application form" in out["reason"]

    def test_it_never_reaches_the_submitter(self, wired):
        wired["questions"] = [{**QUESTIONS_OK[0], "extraction_mode": "fallback"}]
        run()
        assert FakeSubmitter.calls == []


class TestGateUnresolvedRequiredField:
    def test_missing_required_answer_blocks(self, wired):
        wired["questions"] = [{
            **QUESTIONS_OK[0], "field_key": "do_you_have_a_legal_right_to_work_in_the_us",
            "label": "Legal right to work in the US?",
        }]
        out = run()
        assert out["status"] == at.STATUS_NEEDS_USER
        assert out["blocking"]
        assert FakeSubmitter.calls == []

    def test_the_reason_names_the_field(self, wired):
        wired["questions"] = [{
            **QUESTIONS_OK[0], "field_key": "salary_expectation",
            "label": "Expected salary",
        }]
        out = run()
        assert out["blocking"][0]["field_key"] == "salary_expectation"


class TestGateSiteNeedsAHuman:
    def test_blocked_outcome_becomes_needs_user(self, wired):
        """A CAPTCHA is not a failure to retry — only a person clears it."""
        FakeSubmitter.outcome = SubmitOutcome(
            status="blocked", error="page requires a human: recaptcha/api2/anchor")
        out = run()
        assert out["status"] == at.STATUS_NEEDS_USER
        assert "human" in out["reason"]


class TestHappyPath:
    def test_submits_and_records(self, wired):
        out = run()
        assert out["status"] == at.STATUS_SUBMITTED
        session = wired["session"]
        assert len(session.added) == 1, "expected an ApplicationSubmission row"

    def test_application_row_is_marked_submitted(self, wired):
        from app.models.application import Application

        run()
        app_row = wired["rows"][Application.__name__][0]
        assert app_row.status == at.STATUS_SUBMITTED
        assert app_row.submitted_at is not None

    def test_answers_carry_their_provenance(self, wired):
        run()
        sub = wired["session"].added[0]
        assert sub.answers_json[0]["answer"] == "Sarthak"
        assert "confidence" in sub.answers_json[0]

    def test_resume_is_passed_to_the_adapter(self, wired):
        run()
        assert FakeSubmitter.calls[0]["resume_path"]

    def test_confirmation_is_stored(self, wired):
        run()
        assert wired["session"].added[0].confirmation_text == "thank you for applying"


class TestDryRun:
    def test_dry_run_does_not_claim_the_application_was_sent(self, wired):
        """A dry run proves the form can be filled. It did not apply to
        anything, so the row must not say it did."""
        from app.models.application import Application

        out = run(dry_run=True)
        assert out["dry_run"] is True
        app_row = wired["rows"][Application.__name__][0]
        assert app_row.status != at.STATUS_SUBMITTED
        assert app_row.submitted_at is None

    def test_dry_run_still_records_what_it_would_send(self, wired):
        run(dry_run=True)
        assert len(wired["session"].added) == 1

    def test_dry_run_flag_reaches_the_adapter(self, wired):
        run(dry_run=True)
        assert FakeSubmitter.calls[0]["dry_run"] is True


class TestHousekeeping:
    def test_missing_application_is_not_an_error(self, wired):
        wired["rows"] = {k: [] for k in wired["rows"]}
        assert run()["status"] == "not_found"

    def test_session_is_always_closed(self, wired):
        run()
        assert wired["session"].closed is True
