"""F398 — the tester's auto-apply findings of 2026-09-11.

1. Answer Book twins: the real EEO answers sat under eeo_* keys while the
   form's ``gender`` / ``race_ethnicity`` keys had empty placeholder
   twins; the never-infer gate (correctly) refused to guess. Alias lookup
   is now symmetric across every spelling, "[TEST …]" answers are never
   sent, and no placeholder is created when any spelling already exists.
3. A hung submit held a worker child for 4.5 h; the sweeper only ran
   once the deploy restarted the worker. Hard time limits now bound every
   apply/draft/resolve task, and the enqueue stamps the task id.
"""

from app.workers.tasks._answer_prep import alias_group, blocking_gaps, is_placeholder_answer, match_questions_to_answers


def _e(k, a, c="custom"):
    return {"question_key": k, "answer": a, "category": c, "source": "base"}


BOOK = [_e("eeo_gender", "Male"), _e("gender", ""), _e("eeo_race_ethnicity", "Asian"), _e("race_ethnicity", ""),
        _e("eeo_veteran_status", "I am not a protected veteran"), _e("eeocveteran_status", ""),
        _e("eeo_disability_status", "No, I do not have a disability"), _e("eeocdisability_status", ""),
        _e("linkedin_link", "[TEST — replace me] https://linkedin.com/in/x"), _e("linkedin_url", "")]


def test_eeo_answers_are_found_across_spellings_and_empty_twins_do_not_shadow():
    qs = [{"field_key": "race_ethnicity", "label": "Race or Ethnicity", "field_type": "select", "required": True, "options": ["Asian", "White"]},
          {"field_key": "gender", "label": "Gender", "field_type": "select", "required": True, "options": ["Male", "Female"]},
          {"field_key": "eeocveteran_status", "label": "Veteran Status", "field_type": "select", "required": True, "options": ["I am not a protected veteran"]},
          {"field_key": "q9", "label": "Disability Status", "field_type": "select", "required": True, "options": ["No, I do not have a disability"]}]
    m = match_questions_to_answers(qs, BOOK)
    assert [(r["answer"], r["confidence"]) for r in m] == [("Asian", "high"), ("Male", "high"), ("I am not a protected veteran", "high"), ("No, I do not have a disability", "high")]
    assert all(r["never_infer"] for r in m)  # still legal questions — answered only because the saved answer is exact
    assert blocking_gaps(m) == []


def test_alias_groups_are_symmetric():
    assert "eeo_gender" in alias_group("gender") and "gender" in alias_group("eeo_gender")
    assert "eeo_race_ethnicity" in alias_group("race_or_ethnicity")
    assert alias_group("nothing_like_this") == frozenset({"nothing_like_this"})


def test_placeholder_answers_are_never_sent():
    assert is_placeholder_answer("[TEST — replace…] https://x") and is_placeholder_answer("TODO") and is_placeholder_answer("tbd")
    assert not is_placeholder_answer("Test Automation Lead at Acme")  # a real answer that happens to start with "Test"?
    m = match_questions_to_answers([{"field_key": "linkedin", "label": "LinkedIn Profile URL", "field_type": "text", "required": True}], BOOK)[0]
    assert m["answer"] == "" and m["needs_user"]


def test_apply_tasks_have_hard_time_limits_and_stamp_the_task_id():
    import inspect
    from app.workers.tasks import apply_task, draft_answers_task
    from app.api.v1 import applications

    assert apply_task.submit_application_task.time_limit and apply_task.submit_application_task.time_limit <= 900
    assert draft_answers_task.draft_gap_answers_task.time_limit
    assert '"task_id": task.id' in inspect.getsource(applications.submit_application)
