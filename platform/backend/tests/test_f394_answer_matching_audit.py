"""F394 — answer-selection correctness, from an audit of what the new
ATS forms would actually receive from a realistic Answer Book.

Three wrong sends came from substring matching (city ⊂ ethnicity,
location ⊂ relocation, age ⊂ language) and one from the category
fallback filling an OPTIONAL field ("Website" = the first name), which
F385 had only guarded on required fields in the sweep.
"""

from app.workers.tasks._answer_prep import _tokens_overlap, blocking_gaps, match_questions_to_answers


def _e(k, a, c="custom"):
    return {"question_key": k, "answer": a, "category": c, "source": "base"}


def _q(label, key="qx", required=True, ftype="text"):
    return {"field_key": key, "label": label, "field_type": ftype, "required": required}


def _best(label, book, key="qx"):
    r = match_questions_to_answers([_q(label, key)], book)[0]
    return (r["answer"], r["confidence"]) if r["confidence"] in ("high", "medium") else ("", r["confidence"])


class TestSubstringHazards:
    def test_city_never_takes_the_ethnicity_answer(self):
        assert _best("City", [_e("ethnicity", "Prefer not to say")]) == ("", "none")

    def test_location_never_takes_a_relocation_answer(self):
        assert _best("Location", [_e("willing_to_relocate", "No"), _e("relocation", "No")]) == ("", "none")

    def test_age_never_takes_the_language_answer(self):
        assert _best("Age", [_e("language", "English")]) == ("", "none")

    def test_specific_question_does_not_take_the_general_answer(self):
        assert _best("Years of Python experience", [_e("years_experience", "8")]) == ("", "none")
        assert _best("Where are you currently residing? (City and Country)", [_e("country", "UAE")]) == ("", "none")

    def test_qualifier_words_still_match(self):
        assert _best("LinkedIn Profile URL", [_e("linkedin_url", "https://li")]) == ("https://li", "high")  # F398: alias group, exact
        assert _best("Phone Number", [_e("phone", "+1")]) == ("+1", "high")  # F398: alias group
        assert _best("Current location", [_e("location", "Dubai")]) == ("Dubai", "high")  # F398: alias group
        assert _best("What is your notice period?", [_e("notice_period", "30 days")]) == ("30 days", "medium")

    def test_token_overlap_rules(self):
        assert _tokens_overlap("linkedin_url", "linkedin_profile_url")
        assert not _tokens_overlap("city", "ethnicity")
        assert not _tokens_overlap("years_experience", "years_of_python_experience")
        assert not _tokens_overlap("", "phone") and not _tokens_overlap("do_you", "you")


class TestGuessesAreNeverSent:
    def test_optional_field_with_only_a_guess_is_dropped_from_the_submission(self):
        # The category fallback offers first_name for an optional "Website".
        m = match_questions_to_answers([_q("Website", "question_8135802005", required=False, ftype="textarea")],
                                       [_e("first_name", "Sarthak", "personal_info")])[0]
        assert m["confidence"] == "low" and m["answer"] == "" and m["guess"] == "Sarthak"  # F400: a guess is never the answer
        # apply_task's field build skips low-confidence answers (see the
        # `confidence != "low"` filter); mirror that rule here.
        placed = [x for x in [m] if x["field_type"] == "file" or (x["answer"] and x.get("confidence") != "low")]
        assert placed == []
        assert blocking_gaps([{**m, "alternative_group": ""}]) == []  # optional: not a gap, just not sent

    def test_required_guess_is_a_gap_in_both_modes(self):
        # "tell_us" hints the experience category, whose first entry is a guess.
        m = match_questions_to_answers([_q("Tell us about yourself", "tell_us_more", required=True, ftype="textarea")],
                                       [_e("why_are_you_a_great_fit", "Because I ship reliable platforms.", "experience")])[0]
        assert m["confidence"] == "low"
        for unattended in (False, True):
            gaps = blocking_gaps([{**m, "alternative_group": ""}], unattended=unattended)
            assert len(gaps) == 1 and "never sends guesses" in gaps[0]["reason"]

    def test_apply_task_filters_low_confidence(self):
        import inspect
        from app.workers.tasks import apply_task
        assert 'm.get("confidence") != "low"' in inspect.getsource(apply_task)


class TestNewAdapterKeysResolveAtHighConfidence:
    def test_linkedin_and_address_parts(self):
        book = [_e("linkedin_url", "https://li"), _e("zip_code", "18701"), _e("street_address", "1 Main St"), _e("town", "Dubai")]
        for key, label, want in (("linkedin", "LinkedIn Profile URL", "https://li"), ("postcode", "Zip/Postal code", "18701"),
                                 ("address", "Address", "1 Main St"), ("city", "City", "Dubai")):
            r = match_questions_to_answers([_q(label, key)], book)[0]
            assert (r["answer"], r["confidence"]) == (want, "high"), key
