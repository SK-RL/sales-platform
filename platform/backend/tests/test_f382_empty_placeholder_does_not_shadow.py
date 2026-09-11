"""F382 — an empty answer-book placeholder must not shadow a real answer.

Production (Breezy, clarity-rcm): ``auto_populate_answer_book`` wrote an
empty "ats_discovered" entry keyed by the ATS field_key; the exact-key
strategy returned it, and the answer saved under the question's own
text was never consulted — the field stayed a blocker with the answer
sitting right there in the book.
"""

from app.workers.tasks._answer_prep import match_questions_to_answers

Q = [{"field_key": "section_1776898481597_question_0", "label": "Why are you a great fit?", "field_type": "textarea",
      "required": True, "options": [], "description": "", "extraction_mode": "extracted"}]


def _m(entries):
    return match_questions_to_answers(Q, entries)[0]


class TestPlaceholderShadowing:
    def test_labelled_answer_beats_empty_field_key_placeholder(self):
        entries = [
            {"question_key": "section_1776898481597_question_0", "answer": "", "category": "custom", "source": "ats_discovered"},
            {"question_key": "why_are_you_a_great_fit", "answer": "Because…", "category": "custom", "source": "base"},
        ]
        out = _m(entries)
        assert out["answer"] == "Because…" and out["confidence"] == "high" and out["needs_user"] is False

    def test_empty_placeholder_alone_keeps_its_provenance(self):
        out = _m([{"question_key": "section_1776898481597_question_0", "answer": "", "category": "custom", "source": "ats_discovered"}])
        assert out["answer"] == "" and out["match_source"] == "ats_discovered"

    def test_filled_field_key_entry_still_wins(self):
        entries = [
            {"question_key": "section_1776898481597_question_0", "answer": "By key", "category": "custom", "source": "base"},
            {"question_key": "why_are_you_a_great_fit", "answer": "By label", "category": "custom", "source": "base"},
        ]
        assert _m(entries)["answer"] == "By key"
