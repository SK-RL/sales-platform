"""F385 — auto-apply never sends a guess.

Production (Breezy, clarity-rcm): the category fallback auto-filled a
"Summary" textarea with an unrelated answer at confidence "low". A person
on the review screen can see and fix that; the hourly sweep cannot. With
nobody looking, a low-confidence answer on a required field is a gap.
"""

from app.workers.tasks._answer_prep import blocking_gaps

def _m(conf, required=True, answer="guessed text"):
    return [{"field_key": "cSummary", "label": "Summary", "field_type": "textarea", "required": required,
             "answer": answer, "confidence": conf, "needs_user": False, "never_infer": False, "alternative_group": ""}]


def test_attended_review_also_blocks_a_required_guess():
    # F394: the review screen has no inline edit, so a "check it" caption
    # over a guess that then went out unchanged was no protection.
    gaps = blocking_gaps(_m("low"))
    assert len(gaps) == 1 and "never sends guesses" in gaps[0]["reason"]


def test_unattended_blocks_a_required_guess_with_a_plain_reason():
    gaps = blocking_gaps(_m("low"), unattended=True)
    assert len(gaps) == 1 and gaps[0]["field_key"] == "cSummary" and "guess" in gaps[0]["reason"]


def test_unattended_does_not_block_optional_or_confident_answers():
    assert blocking_gaps(_m("low", required=False), unattended=True) == []
    assert blocking_gaps(_m("high"), unattended=True) == []
    assert blocking_gaps(_m("medium"), unattended=True) == []


def test_apply_task_passes_the_flag_for_the_sweep():
    import inspect
    import app.workers.tasks.apply_task as at
    src = inspect.getsource(at.submit_application_task)
    assert 'unattended=(app_row.submission_source == "routine")' in src
