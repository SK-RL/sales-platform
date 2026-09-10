"""F357 — two platforms claimed extraction they never performed.

Found while building the Lever submitter. Before writing DOM automation
I checked what our Lever extractor actually returned, and it wasn't the
application form.

**Lever** never read a form — Lever's public postings API doesn't expose
one. `_fetch_lever_questions` returned a hardcoded seven-field template
and then *invented* the rest from the job description: every entry in
``lists`` became a form field, and any line of ``additionalPlain``
ending in "?" became one too. Verified against a live board
(matchgroup), the "questions" for a real posting were:

    key_responsibilities      'Key Responsibilities'
    required_qualifications   'Required Qualifications'
    work_arrangement          'Work Arrangement'

— the JD's own section headings, presented as things for the candidate
to answer, while the real form was never read. And all of it carried
``extraction_mode="extracted"``, the trust level the F347 gate requires
before submitting unattended. Worse than an honest fallback, because it
was labelled as real extraction.

**Ashby** posts to a genuine application-form API, but that endpoint
requires auth we don't hold — HTTP 401 on every public board tried
(ramp, linear, vanta, openai, supabase, 1password, anyscale). It failed
honestly (the dispatcher marks the fallback) but listing it as supported
was still misleading.

Real extraction coverage is therefore Greenhouse and Recruitee.

The same investigation exposed a second bug: `extraction_mode` is
derived on read from the platform, so a fetch that failed and was cached
as fallback fields would read back as "extracted" — handing the gate a
guessed form wearing an extracted label. Fallback schemas are no longer
cached at all.
"""

import httpx
import pytest

from app.fetchers.questions import (
    SUPPORTED_QUESTION_PLATFORMS,
    _fetch_lever_questions,
    fetch_application_questions,
)


class TestOnlyRealExtractorsAreClaimed:
    def test_supported_is_exactly_the_platforms_that_work(self):
        assert SUPPORTED_QUESTION_PLATFORMS == frozenset({"greenhouse", "recruitee"})

    @pytest.mark.parametrize("platform", ["lever", "ashby"])
    def test_unproven_platforms_are_not_claimed(self, platform):
        assert platform not in SUPPORTED_QUESTION_PLATFORMS


class TestLeverNoLongerFabricates:
    def test_returns_nothing_rather_than_inventing_questions(self):
        assert _fetch_lever_questions("some-id", "matchgroup") == []

    def test_dispatcher_marks_lever_as_a_guess(self):
        fields = fetch_application_questions("lever", "id", "matchgroup")
        assert fields, "still returns the standard set so the UI has something"
        assert all(f["extraction_mode"] == "fallback" for f in fields)

    def test_no_jd_heading_ever_becomes_a_field(self):
        """The concrete regression: 'Key Responsibilities' was a form
        field on every Lever posting."""
        keys = {f["field_key"] for f in fetch_application_questions("lever", "id", "x")}
        for invented in ("key_responsibilities", "required_qualifications", "work_arrangement"):
            assert invented not in keys

    def test_lever_cannot_be_auto_submitted(self):
        """A fallback schema fails the F347 gate, so Lever routes to the
        review queue instead of being submitted against invented fields."""
        from app.services.submitters import auto_submittable_platforms

        assert "lever" not in auto_submittable_platforms()


class TestFallbackSchemasAreNeverCached:
    """`extraction_mode` is derived from the platform on read, so caching
    a guessed schema would launder it into an extracted one."""

    def test_async_path_skips_caching_a_fallback(self, monkeypatch):
        import asyncio

        from app.services import question_service as qs

        added: list = []

        class FakeResult:
            def scalars(self):
                return self

            def all(self):
                return []

        class FakeDB:
            async def execute(self, *a, **kw):
                return FakeResult()

            def add(self, obj):
                added.append(obj)

            async def flush(self):
                return None

            async def rollback(self):
                return None

        monkeypatch.setattr(
            "app.fetchers.questions.fetch_application_questions",
            lambda *a, **kw: [{
                "field_key": "email", "label": "Email", "field_type": "text",
                "required": True, "options": [], "description": "",
                "extraction_mode": "fallback", "fallback_reason": "unsupported_platform",
            }],
        )

        class Job:
            id = "j1"
            platform = "workday"
            external_id = "x"

        out = asyncio.run(qs.get_or_fetch_questions(FakeDB(), Job(), "slug"))
        assert out and out[0]["extraction_mode"] == "fallback"
        assert added == [], "a guessed schema must not be written to job_questions"

    def test_extracted_schemas_are_still_cached(self, monkeypatch):
        import asyncio

        from app.services import question_service as qs

        added: list = []

        class FakeResult:
            def scalars(self):
                return self

            def all(self):
                return []

        class FakeDB:
            async def execute(self, *a, **kw):
                return FakeResult()

            def add(self, obj):
                added.append(obj)

            async def flush(self):
                return None

            async def rollback(self):
                return None

        monkeypatch.setattr(
            "app.fetchers.questions.fetch_application_questions",
            lambda *a, **kw: [{
                "field_key": "email", "label": "Email", "field_type": "text",
                "required": True, "options": [], "description": "",
                "extraction_mode": "extracted",
            }],
        )

        class Job:
            id = "j1"
            platform = "greenhouse"
            external_id = "x"

        asyncio.run(qs.get_or_fetch_questions(FakeDB(), Job(), "slug"))
        assert len(added) == 1
