"""F394 — the gate on the REAL forms of the batch-2 ATSes.

``fixtures/ats_batch2_forms.json`` is what the extractors returned live on
2026-09-11 (made-tech, progress, familiarroads, dover/lexoga/mandrel,
gem/modular). With a realistic Answer Book, these pin down exactly which
fields are sent and which stop for the user — so a change to matching
that starts guessing a legal question, or stops filling a name, fails
here without any network.
"""

import json
from pathlib import Path

import pytest

from app.workers.tasks._answer_prep import blocking_gaps, is_never_infer_field, match_questions_to_answers

FORMS = json.loads((Path(__file__).parent / "fixtures" / "ats_batch2_forms.json").read_text())


def _e(k, a, c="custom"):
    return {"question_key": k, "answer": a, "category": c, "source": "base"}


BOOK = [
    _e("first_name", "Sarthak", "personal_info"), _e("last_name", "Gupta", "personal_info"), _e("email", "s@example.com", "personal_info"),
    _e("phone", "+971501234567", "personal_info"), _e("linkedin_url", "https://www.linkedin.com/in/s", "personal_info"),
    _e("location", "Dubai, United Arab Emirates", "preferences"), _e("country", "United Arab Emirates", "personal_info"),
    _e("city", "Dubai", "personal_info"), _e("address", "Business Bay", "personal_info"), _e("postcode", "00000", "personal_info"),
    _e("work_authorization", "Yes", "work_auth"), _e("do_you_require_sponsorship", "No", "work_auth"), _e("salary", "120000", "preferences"),
    _e("relocation", "No", "preferences"), _e("language", "English", "preferences"), _e("ethnicity", "Prefer not to say", "custom"),
    _e("cover_letter", "I am a DevOps engineer…", "experience"), _e("why_are_you_a_great_fit", "[TEST] fit", "experience"),
]


def _run(name, unattended=True):
    m = match_questions_to_answers(FORMS[name], BOOK)
    gaps = {g["field_key"] for g in blocking_gaps(m, {"resume"}, unattended=unattended)}
    sent = {r["field_key"]: r["answer"] for r in m if r["answer"] and r["confidence"] != "low" and r["field_type"] != "file"}
    return m, gaps, sent


def test_identity_fields_fill_on_every_form():
    for name in FORMS:
        _, _, sent = _run(name)
        assert sent.get("first_name") == "Sarthak" and sent.get("email") == "s@example.com", name


def test_legal_and_protected_questions_stop_on_the_live_labels():
    m, gaps, sent = _run("gem/modular")
    by_label = {r["label"]: r for r in m}
    for label in ("Which countries are you currently legally authorized to work in?",
                  "Do you now or in the future require visa sponsorship for employment?"):
        assert by_label[label]["never_infer"] and by_label[label]["field_key"] in gaps, label
    m, gaps, _ = _run("jobvite/progress")
    gender = next(r for r in m if r["label"] == "Gender Identity")
    assert gender["never_infer"] and gender["field_key"] in gaps
    m, _, _ = _run("dover/mandrel")
    assert next(r for r in m if "legally authorized" in r["label"])["never_infer"]


def test_equality_monitoring_is_never_guessed_even_when_optional():
    m, gaps, sent = _run("pinpoint/made-tech")
    for r in m:
        if r["field_key"].startswith("equality_monitoring["):
            assert is_never_infer_field(r["field_key"], r["label"]) or r["field_key"] in ("equality_monitoring[Education]",), r["field_key"]
            # Only an answer saved under the exact question may be sent —
            # the book's "ethnicity" entry is one; nothing else is.
            if r["field_key"] in sent:
                assert r["confidence"] == "high" and r["question_key"] == "ethnicity", r["field_key"]
    assert not gaps & {r["field_key"] for r in m if r["field_key"].startswith("equality_monitoring[")}  # optional → not gaps


def test_no_wrong_sends_from_lookalike_keys():
    # relocation / language / ethnicity are in the book; none may leak.
    for name in FORMS:
        _, _, sent = _run(name)
        assert "No" not in [v for k, v in sent.items() if k in ("location", "town", "city")], name
        assert "English" not in sent.values(), name
        assert all(k == "equality_monitoring[Ethnicity]" for k, v in sent.items() if v == "Prefer not to say"), name


def test_saved_answer_not_among_options_stops_with_the_options_listed():
    # No exact answer → the consent region is a guess at best → stops as a guess.
    m = match_questions_to_answers(FORMS["jobvite/progress"], BOOK)
    gap = next(g for g in blocking_gaps(m, {"resume"}) if g["field_key"] == "jv_consent_region")
    assert "never sends guesses" in gap["reason"]
    # An exact answer that isn't one of the options → stops with the options listed (F386).
    m = match_questions_to_answers(FORMS["jobvite/progress"], BOOK + [_e("location_of_residence_and_language", "United States")])
    gap = next(g for g in blocking_gaps(m, {"resume"}) if g["field_key"] == "jv_consent_region")
    assert "isn't one of this form's options" in gap["reason"] and "United States of America, English" in gap["reason"]
    # And the exact option text sails through.
    m = match_questions_to_answers(FORMS["jobvite/progress"], BOOK + [_e("location_of_residence_and_language", "United States of America, English")])
    assert "jv_consent_region" not in {g["field_key"] for g in blocking_gaps(m, {"resume"})}


def test_free_text_questions_stop_instead_of_taking_the_test_entry():
    m, gaps, sent = _run("dover/lexoga")
    assert next(r["field_key"] for r in m if r["label"].startswith("Tell us why")) in gaps
    m, gaps, sent = _run("gem/modular")
    assert next(r["field_key"] for r in m if r["label"].startswith("Explain your Cloud Inference")) in gaps
    assert "[TEST] fit" not in sent.values()


@pytest.mark.parametrize("name", sorted(FORMS))
def test_attended_and_unattended_gates_now_agree(name):
    _, att, _ = _run(name, unattended=False)
    _, unatt, _ = _run(name, unattended=True)
    assert att == unatt
