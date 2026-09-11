"""F405 — a draft needs the job description. Prod (Lexoga, Dover): the
"why are you a great fit" draft was a résumé dump because the job had no
stored description; Dover's list endpoint has none. Now the fetchers
carry the posting body, and drafting fetches it on demand.
"""

import inspect
from types import SimpleNamespace

from app.fetchers import dover, gem, hireology, jobvite, pinpoint
from app.services import job_description_service as jds
from app.utils.job_description import extract_description


class TestFetchersCarryTheBody:
    def test_pinpoint_joins_the_sections_with_headers(self):
        raw = {"title": "Bid Manager", "url": "https://x.pinpointhq.com/en/postings/5f8b7dae-56a8-4297-9715-47415e615265",
               "description": "<p>Intro</p>", "key_responsibilities": "<ul><li>Lead bids</li></ul>", "key_responsibilities_header": "What you'll do",
               "skills_knowledge_expertise": "<p>Writing</p>", "skills_knowledge_expertise_header": "Skills", "benefits": ""}
        j = pinpoint.PinpointFetcher()._normalize(raw, "x")
        html, text = extract_description("pinpoint", j["raw_json"])
        assert "<h3>What you'll do</h3>" in html and "Lead bids" in text and "Writing" in text

    def test_hireology_job_description(self):
        raw = {"id": 1, "name": "DCW", "job_description": "<p>Care for clients</p>", "organization": {"name": "FR"}}
        j = hireology.HireologyFetcher()._normalize(raw, "fr")
        assert "Care for clients" in extract_description("hireology", j["raw_json"])[1]

    def test_gem_posting_query_asks_for_the_body(self):
        assert "descriptionHtml" in gem.POSTING_QUERY
        assert 'raw_json"]["description"] = raw["descriptionHtml"]' in inspect.getsource(gem.GemFetcher.fetch_one)

    def test_dover_detail_carries_user_provided_description(self):
        src = inspect.getsource(dover.DoverFetcher.fetch_one)
        assert "user_provided_description" in src

    def test_jobvite_detail_block_is_extracted_even_for_a_listed_job(self):
        page = '<html><title>Progress Careers - SRE</title><div class="jv-job-detail-description" ng-non-bindable><p>We are <b>Progress</b></p></div><div class="other">x</div></html>'
        assert jobvite._description_html(page) == "<p>We are <b>Progress</b></p>"
        assert "listed" in inspect.getsource(jobvite.JobviteFetcher.fetch_one)


class TestEnsureDescription:
    def _job(self, **k):
        base = dict(id="j1", platform="dover", raw_json={}, external_id="dover-abc", url="https://app.dover.com/apply/x/abc")
        base.update(k)
        return SimpleNamespace(**base)

    def test_stored_row_wins(self, monkeypatch):
        job = self._job()
        session = SimpleNamespace(execute=lambda q: SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(text_content="stored text")), commit=lambda: None)
        assert jds.ensure_description_sync(session, job) == ("stored text", "stored")

    def test_fetch_one_fills_and_stores(self, monkeypatch):
        job = self._job()
        stored = []
        session = SimpleNamespace(execute=lambda q: SimpleNamespace(scalar_one_or_none=lambda: None), commit=lambda: None)
        import app.workers.tasks.scan_task as st

        monkeypatch.setattr(st, "_upsert_job_description", lambda s, jid, h, t: stored.append((jid, h, t)))

        class F:
            def fetch_one(self, slug, ext):
                return {"raw_json": {"description": "<p>Lead the backend team</p>"}}

        import app.fetchers as fetchers

        monkeypatch.setitem(fetchers.FETCHER_MAP, "dover", F)
        text, source = jds.ensure_description_sync(session, job, "lexoga")
        assert source == "fetch_one" and "Lead the backend team" in text
        assert stored and stored[0][0] == "j1" and job.raw_json["description"].startswith("<p>")

    def test_page_text_is_the_last_resort(self, monkeypatch):
        job = self._job()
        session = SimpleNamespace(execute=lambda q: SimpleNamespace(scalar_one_or_none=lambda: None), commit=lambda: None)
        import app.workers.tasks.scan_task as st

        monkeypatch.setattr(st, "_upsert_job_description", lambda *a: None)
        import app.fetchers as fetchers

        monkeypatch.setitem(fetchers.FETCHER_MAP, "dover", type("F", (), {"fetch_one": lambda self, s, e: None}))
        monkeypatch.setattr(jds, "_page_text", lambda url: "Backend lead. Python. Trivandrum.")
        assert jds.ensure_description_sync(session, job, "lexoga") == ("Backend lead. Python. Trivandrum.", "page")

    def test_nothing_found_is_empty_not_an_error(self, monkeypatch):
        job = self._job(url="")
        session = SimpleNamespace(execute=lambda q: SimpleNamespace(scalar_one_or_none=lambda: None), commit=lambda: None)
        import app.fetchers as fetchers

        monkeypatch.setitem(fetchers.FETCHER_MAP, "dover", type("F", (), {"fetch_one": lambda self, s, e: None}))
        assert jds.ensure_description_sync(session, job, "lexoga") == ("", "")


def test_draft_tasks_fetch_the_description_first():
    from app.workers.tasks import draft_answers_task, outreach_task

    assert "ensure_description_sync" in inspect.getsource(draft_answers_task._run)
    assert "ensure_description_sync" in inspect.getsource(outreach_task._run)


class TestPromptAnswersTheQuestion:
    def test_why_fit_rule_and_no_jd_rule(self):
        from app.services.answer_drafts import _DRAFT_SYSTEM, _material

        assert "Answer the question that was asked" in _DRAFT_SYSTEM
        assert "Why are you a good fit" in _DRAFT_SYSTEM and "leave out experience the role does not ask for" in _DRAFT_SYSTEM
        assert "not available: only the title is known" in _material("r", [], "T", "C", "")
        assert "not available" not in _material("r", [], "T", "C", "Real JD")

    def test_note_says_when_only_the_title_was_known(self):
        import json
        from types import SimpleNamespace

        from app.services.answer_drafts import draft_answer

        class C:
            def __init__(self):
                self.texts = [json.dumps({"enough_information": True, "answer": "Short."}), json.dumps({"unsupported_claims": []})]
                self.messages = SimpleNamespace(create=lambda **k: SimpleNamespace(content=[SimpleNamespace(type="text", text=self.texts.pop(0))], stop_reason="end_turn", stop_details=None))

        d = draft_answer(question="Why fit?", job_title="Lead", company="Lexoga", job_description="", resume_text="r", book=[], client=C())
        assert "no description was available" in d.note
        d2 = draft_answer(question="Why fit?", job_title="Lead", company="Lexoga", job_description="JD", resume_text="r", book=[], client=C())
        assert "no description" not in d2.note


def test_preview_cell_points_at_the_editor():
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2].joinpath("frontend/src/pages/ApplyReviewPage.tsx").read_text()
    assert "focusGapEditor(q.field_key)" in src and "click to answer it above" in src
