"""F386 — a saved answer that isn't one of the form's options is a gap.

Production, Teamtailor "Locations" (multi-select: USA / Latin America):
the saved answer "Bengaluru, India" passed the gate as "answered", the
adapter could not place it, the run failed after a full browser
session — and Celery retried it twice more for the same result.
"""

from app.workers.tasks._answer_prep import blocking_gaps


def _m(ftype, answer, options, required=True):
    return [{"field_key": "candidate[location_ids][]", "label": "Locations", "field_type": ftype, "required": required,
             "answer": answer, "options": options, "confidence": "high", "needs_user": False, "never_infer": False,
             "alternative_group": ""}]


class TestGate:
    def test_mismatch_is_a_gap_that_names_the_options(self):
        gaps = blocking_gaps(_m("multi_select", "Bengaluru, India", ["USA", "Latin America"]))
        assert len(gaps) == 1 and "USA, Latin America" in gaps[0]["reason"] and "Bengaluru" in gaps[0]["reason"]

    def test_matching_option_passes(self):
        assert blocking_gaps(_m("select", "usa", ["USA", "Latin America"])) == []
        assert blocking_gaps(_m("multi_select", "USA; Latin America", ["USA", "Latin America"])) == []

    def test_dict_options_and_synonyms(self):
        assert blocking_gaps(_m("select", "true", [{"value": "1", "label": "Yes"}, {"value": "0", "label": "No"}])) == []
        assert blocking_gaps(_m("select", "Prefer not to say", [{"value": "1", "label": "Yes"}, {"value": "0", "label": "No"}]))

    def test_optional_or_free_text_untouched(self):
        assert blocking_gaps(_m("multi_select", "Bengaluru", ["USA"], required=False)) == []
        assert blocking_gaps(_m("text", "Bengaluru", [])) == []


class TestRetryPolicy:
    def test_unplaceable_fields_are_not_retried(self):
        import inspect
        import app.workers.tasks.apply_task as at
        src = inspect.getsource(at.submit_application_task)
        assert 'startswith("required fields could not be")' in src
