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


def test_celery_health_endpoint_registered():
    from tests._routes import registered_paths

    assert "/api/v1/monitoring/celery" in registered_paths()


def test_f400_guess_never_becomes_the_answer_even_on_optional_fields():
    """Round 2, finding 1: a category-fallback guess reached Personio's
    "Upload Cover letter" and Breezy's "Experience Summary" as ``answer``
    with needs_user=false. It now travels as ``guess`` and ``answer`` is
    empty everywhere — preview, prepared_answers, submitter."""
    book = [_e("have_you_managed_multi_language_build_pipelines", "Partly. I have run pipelines covering Python…", "experience")]
    qs = [{"field_key": "cover_letter", "label": "Upload Cover letter", "field_type": "textarea", "required": False},
          {"field_key": "cSummary", "label": "Experience Summary", "field_type": "textarea", "required": False},
          {"field_key": "q1", "label": "Tell us about yourself", "field_type": "textarea", "required": True}]
    m = match_questions_to_answers(qs, book)
    for r in m:
        assert r["answer"] == "", r["label"]
    guessed = [r for r in m if r["guess"]]
    assert guessed and all(r["confidence"] == "low" for r in guessed)
    gaps = {g["field_key"]: g["reason"] for g in blocking_gaps(m)}
    assert "cover_letter" not in gaps and "cSummary" not in gaps  # optional → blank, not a gap
    assert "q1" in gaps  # required and unanswered either way — a guess never satisfies it


def test_f400_stale_pass_is_reported_as_stale():
    from app.workers.tasks._answer_prep import GATE_RULES_CHANGED_AT, gate_result_is_stale

    assert gate_result_is_stale({"gate": "passed", "checked_at": "2026-09-10T09:00:00+00:00"})
    assert not gate_result_is_stale({"gate": "passed", "checked_at": "2026-09-11T14:00:00+00:00"})
    assert not gate_result_is_stale({"gate": "blocked", "checked_at": "2026-09-10T09:00:00+00:00"})
    assert GATE_RULES_CHANGED_AT.endswith("+00:00")


def test_f401_periodic_sweeps_leave_the_default_queue_and_do_not_pile_up():
    """Prod, 13:46 UTC: two copies of resolve_aggregator_links running side
    by side on the default worker, 31 tasks queued behind them, a dry run
    PENDING for 30 minutes. Long periodic jobs now run on the heavy worker,
    expire if a restart delays them, and the aggregator run is non-reentrant.
    A submit is never redelivered after a crash (that could apply twice)."""
    from app.workers.celery_app import celery_app
    from app.workers.tasks import aggregator_task, apply_task, draft_answers_task

    routes = celery_app.conf.task_routes
    for name in ("app.workers.tasks.scan_task.scan_all_platforms", "app.workers.tasks.aggregator_task.resolve_aggregator_links",
                 "app.workers.tasks.career_page_task.check_career_pages"):
        assert routes[name] == {"queue": "heavy"}, name
    # F407 — interactive tasks now have their own queue, consumed ahead of default; never heavy.
    assert routes["app.workers.tasks.apply_task.submit_application_task"] == {"queue": "interactive"}
    beat = celery_app.conf.beat_schedule
    assert beat["resolve_aggregator_links"]["options"]["expires"] <= 3600
    assert beat["scan_all_platforms"]["options"]["expires"] <= 8 * 3600
    assert apply_task.submit_application_task.acks_late is False
    assert draft_answers_task.draft_gap_answers_task.acks_late is False
    assert aggregator_task.resolve_aggregator_links.acks_late is False
    import inspect
    src = inspect.getsource(aggregator_task.resolve_aggregator_links)
    assert 'acquire_scan_lock_sync("aggregator"' in src and 'release_scan_lock("aggregator")' in src


def test_f401_periodic_tasks_have_time_limits_and_revoke_endpoint_exists():
    from app.workers.tasks import aggregator_task, career_page_task, scan_task
    from tests._routes import registered_paths

    for t in (aggregator_task.resolve_aggregator_links, aggregator_task.resolve_one_aggregator_job,
              career_page_task.check_career_pages, scan_task.scan_all_platforms):
        assert t.time_limit and t.acks_late is False, t.name
    assert "/api/v1/monitoring/celery/revoke/{task_id}" in registered_paths()


def test_every_task_module_is_imported_by_the_registry():
    """Prod, 14:35 UTC: the worker rejected draft_gap_answers_task as
    unregistered — autodiscover looks for ``app.workers.tasks.tasks``, so
    registration is the explicit import list in the package __init__.
    Every *_task.py module must be there."""
    import importlib
    import pkgutil

    import app.workers.tasks as pkg
    from app.workers.celery_app import celery_app

    importlib.import_module("app.workers.tasks")
    registered = set(celery_app.tasks.keys())
    for m in pkgutil.iter_modules(pkg.__path__):
        if not m.name.endswith("_task") or m.name.startswith("_"):
            continue
        from celery import Task

        mod = importlib.import_module(f"app.workers.tasks.{m.name}")
        for n, v in vars(mod).items():
            if isinstance(v, Task):
                assert v.name in registered, f"{v.name} is not registered — add {m.name} to app/workers/tasks/__init__.py"


def test_f401_sweeper_returns_an_interrupted_dry_run_to_prepared(monkeypatch):
    from types import SimpleNamespace
    from app.workers.tasks import apply_task

    dry = SimpleNamespace(status="in_flight", platform_response={"queued": {"task_id": "t", "dry_run": True, "at": "x"}, "drafts": {"q": {"text": "keep"}}})
    real = SimpleNamespace(status="in_flight", platform_response={"queued": {"task_id": "t2", "dry_run": False, "at": "x"}})

    class Q:
        def scalars(self): return self
        def all(self): return [dry, real]

    class S:
        def execute(self, stmt): return Q()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(apply_task, "SyncSession", lambda: S())
    out = apply_task.sweep_stuck_in_flight()
    assert out["swept"] == 2
    assert dry.status == "prepared" and dry.platform_response["gate"] == "interrupted" and dry.platform_response["drafts"]["q"]["text"] == "keep"
    assert "queued" not in dry.platform_response
    assert real.status == "failed" and "check the employer" in real.platform_response["error"]
