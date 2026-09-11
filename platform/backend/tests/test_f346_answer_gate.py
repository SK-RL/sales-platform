"""F346 — never answer a legal / EEO / compensation question by inference.

Background
----------
``_find_best_match`` has five strategies in descending precision:
exact key, alias, normalised label, substring, category fallback. The
last two are fuzzy. Before F346 they applied to every field, so a
Greenhouse screener like ``do_you_have_a_legal_right_to_work_in_the_us``
would hit ``_CATEGORY_HINTS["authorized"] -> work_auth`` and return
whichever work-auth answer sorted first — e.g. an *India* work
authorization answer used to assert US work eligibility.

It was marked ``confidence="low"``, but nothing downstream read
``confidence``: ``preview_job_questions`` only counted ``"high"`` for a
display stat, and ``/applications/readiness`` computed ``can_apply``
from resume + credentials alone. So the wrong answer was submittable.

These tests pin the three guarantees:
  1. never-infer fields refuse fuzzy matches (but still take exact ones)
  2. ordinary fields keep their fuzzy matching
  3. a guessed (fallback) form schema is never "safe to auto submit"
"""

from app.fetchers.questions import (
    SUPPORTED_QUESTION_PLATFORMS,
    fetch_application_questions,
)
from app.workers.tasks._answer_prep import (
    blocking_gaps,
    is_never_infer_field,
    match_questions_to_answers,
)


# The exact answer-book shape the API hands the matcher: an India work
# authorization entry and nothing about the US.
_INDIA_WORK_AUTH = [
    {
        "question_key": "work_authorization",
        "answer": "Yes - permanent resident of India",
        "category": "work_auth",
        "source": "manual_required",
    },
    {
        "question_key": "linkedin",
        "answer": "https://linkedin.com/in/example",
        "category": "personal_info",
        "source": "manual",
    },
]


def _q(field_key, label="", required=True, **extra):
    return {
        "field_key": field_key,
        "label": label,
        "field_type": "text",
        "required": required,
        "options": [],
        "description": "",
        **extra,
    }


class TestNeverInferClassification:
    def test_us_work_authorization_is_never_infer(self):
        assert is_never_infer_field("do_you_have_a_legal_right_to_work_in_the_us", "")

    def test_sponsorship_is_never_infer(self):
        assert is_never_infer_field(
            "will_you_now_or_in_the_future_require_immigration_sponsorship", ""
        )

    def test_eeo_fields_are_never_infer(self):
        for key in ("gender", "race", "veteran_status", "disability_status"):
            assert is_never_infer_field(key, ""), key

    def test_salary_is_never_infer(self):
        assert is_never_infer_field("desired_salary", "")

    def test_opaque_key_is_caught_via_label(self):
        """Workday uses meaningless keys and puts the question in the label."""
        assert is_never_infer_field(
            "primaryQuestion--1",
            "Are you legally authorized to work in the United States?",
        )

    def test_rate_inside_a_word_is_not_compensation(self):
        """F366 regression: a live Ashby question 'Please elaborate on
        your experience…' was flagged because "rate" matched inside
        "elaborate". Only pay-rate phrasings should."""
        assert not is_never_infer_field("q1", "Please elaborate on your experience building software")
        assert not is_never_infer_field("q2", "Describe how you operate under pressure")
        assert is_never_infer_field("q3", "What is your expected hourly rate?")

    def test_ordinary_fields_are_not_never_infer(self):
        for key in ("first_name", "linkedin_url", "website", "how_did_you_hear"):
            assert not is_never_infer_field(key, ""), key


class TestFuzzyMatchingIsRefusedForSensitiveFields:
    def test_us_work_auth_does_not_inherit_india_answer(self):
        """The exact regression: India work-auth must not answer a US question."""
        matched = match_questions_to_answers(
            [_q("do_you_have_a_legal_right_to_work_in_the_us")],
            _INDIA_WORK_AUTH,
        )
        (field,) = matched
        assert field["answer"] == ""
        assert field["confidence"] == "none"
        assert field["never_infer"] is True
        assert field["needs_user"] is True

    def test_sponsorship_question_is_left_unanswered(self):
        matched = match_questions_to_answers(
            [_q("will_you_require_immigration_sponsorship_in_the_united_states")],
            _INDIA_WORK_AUTH,
        )
        assert matched[0]["answer"] == ""
        assert matched[0]["needs_user"] is True

    def test_exact_match_still_answers_a_sensitive_field(self):
        """The guard blocks inference, not answering. An exact saved
        answer is exactly the case we want to honour."""
        matched = match_questions_to_answers(
            [_q("work_authorization")], _INDIA_WORK_AUTH
        )
        (field,) = matched
        assert field["answer"] == "Yes - permanent resident of India"
        assert field["confidence"] == "high"
        assert field["needs_user"] is False


class TestOrdinaryFieldsKeepFuzzyMatching:
    def test_alias_match_still_works(self):
        matched = match_questions_to_answers(
            [_q("linkedin_url")], _INDIA_WORK_AUTH
        )
        assert matched[0]["answer"] == "https://linkedin.com/in/example"
        assert matched[0]["confidence"] == "high"
        assert matched[0]["needs_user"] is False

    def test_unmatched_reports_none_not_low(self):
        """`low` now means 'category fallback guessed'; absent is `none`."""
        matched = match_questions_to_answers(
            [_q("favourite_programming_paradigm")], _INDIA_WORK_AUTH
        )
        assert matched[0]["confidence"] == "none"

    def test_optional_unmatched_field_does_not_block(self):
        matched = match_questions_to_answers(
            [_q("website", required=False)], []
        )
        assert matched[0]["needs_user"] is False
        assert blocking_gaps(matched) == []


class TestBlockingGaps:
    def test_sensitive_gap_explains_itself(self):
        matched = match_questions_to_answers(
            [_q("do_you_have_a_legal_right_to_work_in_the_us", "Legal right to work?")],
            _INDIA_WORK_AUTH,
        )
        (gap,) = blocking_gaps(matched)
        assert gap["field_key"] == "do_you_have_a_legal_right_to_work_in_the_us"
        assert gap["label"] == "Legal right to work?"
        assert "legal or protected-class" in gap["reason"]

    def test_fully_answered_form_has_no_gaps(self):
        matched = match_questions_to_answers(
            [_q("work_authorization"), _q("linkedin_url")], _INDIA_WORK_AUTH
        )
        assert blocking_gaps(matched) == []


class TestFallbackSchemaIsMarked:
    def test_unsupported_platform_returns_fallback_mode(self):
        fields = fetch_application_questions("workday", "job-123", "acme")
        assert fields, "expected the standard field set"
        assert all(f["extraction_mode"] == "fallback" for f in fields)
        assert all(f["fallback_reason"] == "unsupported_platform" for f in fields)

    def test_workday_is_not_claimed_as_supported(self):
        """We list Workday jobs but cannot extract their forms. If this
        starts failing, a real Workday extractor landed — good, but the
        auto-submit gate below needs revisiting with it."""
        assert "workday" not in SUPPORTED_QUESTION_PLATFORMS
        assert "greenhouse" in SUPPORTED_QUESTION_PLATFORMS

    def test_fallback_fields_propagate_through_the_matcher(self):
        fields = fetch_application_questions("workday", "job-123", "acme")
        matched = match_questions_to_answers(fields, _INDIA_WORK_AUTH)
        assert all(m["extraction_mode"] == "fallback" for m in matched)
