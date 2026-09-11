"""F396 — role-specific free-text answers are drafted for approval, never sent.

Draft from résumé + Answer Book + job description on claude-opus-5, then a
second pass lists claims the material doesn't support. Legal, compensation,
protected-class and identity fields are never drafted. The draft lands in
platform_response.drafts; only what the user saves via /answer-gap reaches
the Answer Book and, from there, the form.
"""

import json
from types import SimpleNamespace

from app.services.answer_drafts import Draft, draft_answer, draftable
from tests._routes import registered_paths


class _Client:
    """Scripted responses: first call is the draft, second the check."""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        t = self.texts.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=t)],
                               stop_reason="end_turn", stop_details=None)


ARGS = dict(question="Explain your Cloud Inference experience in 3-4 lines.", job_title="Cloud Inference Engineer", company="Modular",
            job_description="Serve LLMs at scale on GPUs.", resume_text="8 years DevOps. Ran GPU inference clusters on Kubernetes at Acme (2021-2024).",
            book=[{"question": "Skills", "question_key": "skills", "answer": "Kubernetes, Terraform, AWS"}])


def test_grounded_draft_with_clean_check():
    c = _Client(json.dumps({"enough_information": True, "answer": "At Acme I ran GPU inference clusters on Kubernetes for three years.", "facts_used": ["Acme GPU clusters"]}),
                json.dumps({"unsupported_claims": []}))
    d = draft_answer(client=c, **ARGS)
    assert d.enough_information and "Acme" in d.text and d.unsupported_claims == [] and "Every claim traced" in d.note
    assert len(c.calls) == 2 and c.calls[0]["model"] == "claude-opus-5"
    assert "Never invent" in c.calls[0]["system"] and "CANDIDATE RÉSUMÉ" in c.calls[0]["messages"][0]["content"]


def test_unsupported_claims_are_revised_away_when_possible():
    # F403: draft → check (1 claim) → revise → check (clean) = a clean draft with a note.
    c = _Client(json.dumps({"enough_information": True, "answer": "I led a 40-person platform team at Acme.", "facts_used": []}),
                json.dumps({"unsupported_claims": ["led a 40-person platform team"]}),
                json.dumps({"answer": "At Acme I ran GPU inference clusters on Kubernetes."}),
                json.dumps({"unsupported_claims": []}))
    d = draft_answer(client=c, **ARGS)
    assert d.text == "At Acme I ran GPU inference clusters on Kubernetes." and d.unsupported_claims == [] and "revised to drop 1 claim" in d.note
    assert len(c.calls) == 4 and "correcting" in c.calls[2]["system"]


def test_unsupported_claims_are_flagged_when_revision_still_fails():
    c = _Client(json.dumps({"enough_information": True, "answer": "I led a 40-person platform team at Acme.", "facts_used": []}),
                json.dumps({"unsupported_claims": ["led a 40-person platform team"]}),
                json.dumps({"answer": "I led a 30-person team at Acme."}),
                json.dumps({"unsupported_claims": ["led a 30-person team"]}))
    d = draft_answer(client=c, **ARGS)
    assert d.text == "I led a 30-person team at Acme." and d.unsupported_claims == ["led a 30-person team"] and "could not be traced" in d.note


def test_not_enough_information_means_no_draft():
    c = _Client(json.dumps({"enough_information": False, "answer": "", "facts_used": []}))
    d = draft_answer(client=c, **ARGS)
    assert d.text == "" and not d.enough_information and "don't cover this" in d.note and len(c.calls) == 1


def test_failures_never_break_the_apply_path():
    class Boom:
        messages = SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("network")))
    d = draft_answer(client=Boom(), **ARGS)
    assert isinstance(d, Draft) and d.error == "draft failed" and d.text == ""


def test_em_dashes_are_removed_from_drafts():
    c = _Client(json.dumps({"enough_information": True, "answer": "I ran clusters — on Kubernetes.", "facts_used": []}), json.dumps({"unsupported_claims": []}))
    assert "—" not in draft_answer(client=c, **ARGS).text


def test_employer_asking_for_own_words_is_honoured():
    from app.services.answer_drafts import employer_wants_own_words

    q = "Explain your Cloud Inference experience in 3-4 lines. Please refrain from using AI to complete answer this questions."
    assert employer_wants_own_words(q) and not draftable({"field_key": "x", "label": q, "field_type": "textarea"})
    assert employer_wants_own_words("Answer in your own words") and employer_wants_own_words("", "Do not use ChatGPT")
    assert employer_wants_own_words("During this application process I agree to use only my own words. I understand…")  # Canonical
    assert not employer_wants_own_words("Describe your experience with AI tooling")


def test_only_role_specific_free_text_is_draftable():
    assert draftable({"field_key": "q1", "label": "Why are you a great fit?", "field_type": "textarea"})
    assert draftable({"field_key": "cXVl", "label": "Explain your Cloud Inference experience", "field_type": "text"})
    assert not draftable({"field_key": "q2", "label": "Do you require visa sponsorship?", "field_type": "textarea"})
    assert not draftable({"field_key": "q3", "label": "Expected salary", "field_type": "text"})
    assert not draftable({"field_key": "linkedin", "label": "LinkedIn URL", "field_type": "text"})
    assert not draftable({"field_key": "q4", "label": "Office?", "field_type": "select"})


def test_endpoints_and_wiring():
    import inspect
    from app.workers.tasks import apply_task
    from app.api.v1 import applications

    assert "/api/v1/applications/{app_id}/answer-gap" in registered_paths()
    assert "draft_gap_answers_task.apply_async" in inspect.getsource(apply_task._halt)
    assert "draft_gap_answers_task.apply_async" in inspect.getsource(applications.prepare_application)


def test_halt_keeps_existing_drafts_and_enqueues(_no_broker_for_drafts):
    from app.workers.tasks.apply_task import _halt

    class S:
        def commit(self): pass
    row = SimpleNamespace(id="a1", status="prepared", platform_response={"drafts": {"q1": {"text": "kept"}}})
    _halt(S(), row, "1 required field(s) need your answer", [{"field_key": "q1", "label": "Q", "reason": "x"}])
    assert row.platform_response["drafts"]["q1"]["text"] == "kept" and row.platform_response["blocking"][0]["field_key"] == "q1"
    assert _no_broker_for_drafts and _no_broker_for_drafts[0][1]["args"] == ["a1"]
