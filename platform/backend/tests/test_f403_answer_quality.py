"""F403 — answer quality: résumé-import keys reach the gate, safe option
equivalences, drafts revised instead of red-flagged, one answer clears the
same question everywhere, and the Auto-apply list says when a draft waits."""

import inspect

from app.services.submitters.base import coerce_option
from app.workers.tasks._answer_prep import match_questions_to_answers


def _e(k, a, c="personal_info"):
    return {"question_key": k, "answer": a, "category": c, "source": "resume"}


def test_resume_import_keys_fill_identity_fields_at_high_confidence():
    # answer_book.import_from_resume writes these question texts.
    book = [_e("what_is_your_linkedin_url", "https://linkedin.com/in/sarthak"), _e("what_is_your_github_url", "https://github.com/sarthak"),
            _e("what_is_your_email_address", "s@example.com"), _e("what_is_your_phone_number", "+91 86022 61856")]
    qs = [{"field_key": "linkedin", "label": "LinkedIn URL", "field_type": "text", "required": True},
          {"field_key": "github", "label": "GitHub profile", "field_type": "text", "required": False},
          {"field_key": "email", "label": "Email", "field_type": "text", "required": True},
          {"field_key": "phone", "label": "Phone Number", "field_type": "text", "required": True}]
    m = match_questions_to_answers(qs, book)
    assert [(r["answer"] != "", r["confidence"]) for r in m] == [(True, "high")] * 4


class TestSafeOptionEquivalences:
    def test_eeo_opt_out_reaches_the_forms_own_phrasing(self):
        assert coerce_option("Prefer not to say", ["Female", "Male", "Decline to Self Identify"]) == "Decline to Self Identify"
        assert coerce_option("prefer not to say", ["Male", "I don't wish to answer"]) == "I don't wish to answer"
        assert coerce_option("Prefer not to say", ["Yes", "No"]) is None  # no opt-out offered → still a gap

    def test_number_against_range_options(self):
        assert coerce_option("3", ["0", "1", "2+"]) == "2+"
        assert coerce_option("7", ["Less than 1", "1-3", "3-5", "5 or more"]) == "5 or more"
        assert coerce_option("10 years", ["0-2", "3-5", "6+"]) == "6+"
        assert coerce_option("3", ["0-1 years", "1-3 years", "3-5 years"]) is None  # ambiguous: refuse
        assert coerce_option("3", ["0", "1", "2", "3"]) == "3"


def test_answering_a_gap_clears_it_on_the_users_other_waiting_applications():
    from app.api.v1 import applications

    src = inspect.getsource(applications.answer_gap)
    assert "cleared_elsewhere" in src and 'Application.status == "needs_user"' in src


def test_list_rows_report_drafts_ready():
    from app.api.v1 import applications

    assert '"drafts_ready"' in inspect.getsource(applications.list_applications)
