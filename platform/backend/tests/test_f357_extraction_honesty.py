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

Real extraction coverage after this change was Greenhouse and
Recruitee. F358 then gave Lever a genuine extractor (it reads the
server-rendered apply page), so it is legitimately supported again — the
tests below assert the RULE, not that snapshot.

The same investigation exposed a second bug: `extraction_mode` is
derived on read from the platform, so a fetch that failed and was cached
as fallback fields would read back as "extracted" — handing the gate a
guessed form wearing an extracted label. Fallback schemas are no longer
cached at all.
"""

from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS


class TestOnlyRealExtractorsAreClaimed:
    """The durable rule, not a snapshot of the platform list: a platform
    may only appear in SUPPORTED_QUESTION_PLATFORMS if something in the
    dispatcher actually reads its form.

    (F358 subsequently gave Lever a real extractor — it reads the
    server-rendered apply page — so it is legitimately back on the list.
    Ashby is not: its form endpoint 401s on every public board.)
    """

    def test_ashby_is_not_claimed(self):
        assert "ashby" not in SUPPORTED_QUESTION_PLATFORMS

    def test_every_supported_platform_has_a_wired_extractor(self):
        import inspect

        from app.fetchers import questions as q

        src = inspect.getsource(q.fetch_application_questions)
        for platform in SUPPORTED_QUESTION_PLATFORMS:
            assert f'"{platform}":' in src, (
                f"{platform} is claimed as supported but is not wired into "
                "the dispatcher, so it can never be extracted"
            )

    def test_no_platform_is_wired_without_being_claimed(self):
        """The inverse leak: a wired extractor whose platform isn't in
        SUPPORTED gets stamped `extracted` by the dispatcher but reads
        back as `fallback` from cache."""
        import inspect
        import re

        from app.fetchers import questions as q

        src = inspect.getsource(q.fetch_application_questions)
        block = src[src.index("fetchers = {"):src.index("}", src.index("fetchers = {"))]
        wired = set(re.findall(r'"([a-z_]+)":', block))
        assert wired <= set(SUPPORTED_QUESTION_PLATFORMS), wired - set(SUPPORTED_QUESTION_PLATFORMS)


class TestLeverNoLongerFabricates:
    """Whatever Lever's extractor does, it must never manufacture a
    question out of job-description prose."""

    def test_no_jd_heading_ever_becomes_a_field(self):
        """The concrete regression: 'Key Responsibilities' was a form
        field on every Lever posting."""
        import inspect

        from app.fetchers import questions as q

        src = inspect.getsource(q._fetch_lever_questions)
        # The fabrication read `lists` and split `additionalPlain` on "?".
        assert "additionalPlain" not in src
        assert 'data.get("lists"' not in src

    def test_extraction_is_from_the_apply_page_not_the_posting_api(self):
        import inspect

        from app.fetchers import questions as q

        src = inspect.getsource(q._fetch_lever_questions)
        assert "/apply" in src

    def test_lever_still_has_no_submitter(self):
        """Extraction and submission are separate capabilities."""
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
