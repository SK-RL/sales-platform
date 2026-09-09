"""``POST /applications/record`` — the create step for the browser-side
apply lane (the ``job-apply`` Claude skill under ``.claude/skills/``).

Why it exists: the skill fills the ATS form in the user's own Chrome and
must store what it typed against that job's Application so the answers
are recallable from Job Detail. ``/prepare`` can't serve that lane — it
requires a stored ``PlatformCredential`` for the ATS and fetches the
questions itself. After a confirmed submit the skill calls the existing
``/{id}/confirm-submitted``; ``/record`` must therefore never advance
``status`` past ``prepared`` or it would race that handler's side
effects (review row, pipeline entry, scoring feedback, usage counts).

Source-level tests, no DB — same style as test_f298_applications_pydantic.
"""
from __future__ import annotations

import inspect
import os

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-job-apply")


def test_record_route_registered():
    from app.api.v1.router import api_router

    paths = {
        (m, r.path)
        for r in api_router.routes
        for m in (getattr(r, "methods", None) or set())
    }
    assert ("POST", "/api/v1/applications/record") in paths


# --- request schema ---------------------------------------------------------

def test_record_request_rejects_extra_fields():
    """``extra="forbid"`` — a typo like ``jb_url`` must 422, not vanish."""
    import pydantic
    from app.api.v1.applications import RecordApplicationRequest

    RecordApplicationRequest(job_url="https://boards.greenhouse.io/acme/jobs/1")
    with pytest.raises(pydantic.ValidationError):
        RecordApplicationRequest(jb_url="https://x")  # type: ignore[call-arg]


def test_record_request_caps_answers_at_200():
    import pydantic
    from app.api.v1.applications import RecordApplicationRequest

    ok = [{"label": f"q{i}", "answer": "a"} for i in range(200)]
    RecordApplicationRequest(job_url="https://x/1", answers=ok)
    with pytest.raises(pydantic.ValidationError):
        RecordApplicationRequest(job_url="https://x/1", answers=ok + [{"label": "q", "answer": "a"}])


def test_record_request_answers_accept_label_and_field_type():
    """Answers reuse ``ApplicationAnswer`` (``extra="allow"``) so the skill
    can send the label it saw and the control type without a 422."""
    from app.api.v1.applications import RecordApplicationRequest

    req = RecordApplicationRequest(
        job_id="00000000-0000-0000-0000-000000000001",
        answers=[{"label": "Why do you want to work here?", "answer": "…", "field_type": "textarea", "required": True}],
    )
    dumped = req.answers[0].model_dump()
    assert dumped["label"] == "Why do you want to work here?"
    assert dumped["field_type"] == "textarea"


# --- normaliser: what gets stored must be what Job Detail renders ---------

def test_normalize_fills_full_prepared_answer_shape():
    from app.api.v1.applications import _normalize_recorded_answers

    rows = _normalize_recorded_answers([
        {"label": "First Name", "answer": "Ada", "field_type": "text", "required": True},
    ])
    assert len(rows) == 1
    row = rows[0]
    # Every key the frontend ``PreparedAnswer`` interface reads.
    for key in ("field_key", "label", "field_type", "required", "options",
                "description", "answer", "match_source", "question_key", "confidence"):
        assert key in row, f"missing {key}"
    assert row["question_key"] == "first_name"
    assert row["field_key"] == "first_name"
    assert row["match_source"] == "override"   # operator-chosen, not matcher-chosen
    assert row["confidence"] == "high"
    assert row["required"] is True
    assert row["answer"] == "Ada"


def test_normalize_keeps_explicit_question_key_and_coerces_bad_field_type():
    from app.api.v1.applications import _normalize_recorded_answers

    rows = _normalize_recorded_answers([
        {"question_key": "greenhouse_custom_field_42", "label": "Anything else?", "answer": "no",
         "field_type": "wizard"},   # not a real type → falls back to text
    ])
    assert rows[0]["question_key"] == "greenhouse_custom_field_42"
    assert rows[0]["field_type"] == "text"


def test_normalize_drops_rows_with_no_label_or_key():
    """An unlabelled blank would be stored as an unrecallable row —
    drop it rather than pollute the snapshot."""
    from app.api.v1.applications import _normalize_recorded_answers

    assert _normalize_recorded_answers([{"answer": "orphan"}]) == []


def test_normalize_accepts_question_as_label_alias():
    """The skill sometimes sends ``question`` (the /sync-answers and
    confirm-submitted vocabulary) instead of ``label``."""
    from app.api.v1.applications import _normalize_recorded_answers

    rows = _normalize_recorded_answers([{"question": "Notice period", "answer": "30 days"}])
    assert rows[0]["label"] == "Notice period"
    assert rows[0]["question_key"] == "notice_period"


# --- handler invariants (source-level) --------------------------------------

def test_record_never_advances_status_past_prepared():
    """``confirm-submitted`` owns the ``applied`` transition and its side
    effects. ``/record`` writing ``applied``/``submitted`` would race it."""
    from app.api.v1.applications import record_application

    src = inspect.getsource(record_application)
    assert 'status="prepared"' in src
    for forbidden in ('status = "applied"', 'status="applied"', 'status = "submitted"', 'status="submitted"'):
        assert forbidden not in src, f"/record must not set {forbidden}"
    assert "confirm-submitted" in src  # the docstring points readers at the owner


def test_record_refuses_to_overwrite_a_submitted_application():
    from app.api.v1.applications import record_application

    src = inspect.getsource(record_application)
    assert 'existing.status != "prepared"' in src
    assert "status_code=409" in src


def test_record_requires_exactly_one_of_job_id_or_job_url():
    from app.api.v1.applications import record_application

    src = inspect.getsource(record_application)
    assert "(body.job_id is None) == (body.job_url is None)" in src


def test_record_requires_active_resume_like_prepare_does():
    """``Application.resume_id`` is NOT NULL; mirror /prepare's guard and
    its exact wording so the skill can match on one message."""
    from app.api.v1.applications import record_application, prepare_application

    rec = inspect.getsource(record_application)
    prep = inspect.getsource(prepare_application)
    msg = "No active resume selected. Please switch to a resume first."
    assert msg in rec and msg in prep


def test_record_url_lookup_escapes_like_metacharacters():
    """A pasted URL containing ``%`` or ``_`` must not turn into a wildcard
    that matches some other company's posting."""
    from app.api.v1.applications import record_application

    src = inspect.getsource(record_application)
    assert "escape_like(base)" in src


def test_record_is_tagged_as_the_claude_routine_lane():
    """ApplicationsPage renders an "Auto" badge for ``claude_routine`` and
    filters by ``submission_source``; both must be set on create."""
    from app.api.v1.applications import record_application

    src = inspect.getsource(record_application)
    assert 'apply_method="claude_routine"' in src
    assert 'submission_source="routine"' in src
