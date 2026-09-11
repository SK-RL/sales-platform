"""F370 — a guessed template cached before F357 must not read as extracted.

Production, Northflank "Cloud Infrastructure Engineer" (Ashby): the
preview showed first_name / last_name / email / phone / resume /
cover_letter / linkedin_url / website — the 8 standard fallback fields —
stamped ``extraction_mode: extracted`` and ``safe_to_auto_submit: true``.
The rows were written when fallbacks were still cached (pre-F357) and
Ashby was unsupported; ``_cached_row_to_dict`` derives the mode from the
platform, which is now supported, so the guess was laundered. The real
form's keys are ``_systemfield_name`` and UUIDs — the adapter would have
placed nothing.

``job_questions`` has no "guessed" column (the F363 pattern again), so
the template is recognised by its exact (key, label) set and treated as
a cache miss.
"""

import inspect

from app.fetchers.questions import _STANDARD_FIELDS
from app.services import question_service as qs


class Row:
    def __init__(self, field_key, label):
        self.field_key, self.label = field_key, label


def _template():
    return [Row(f["field_key"], f["label"]) for f in _STANDARD_FIELDS]


class TestRecognition:
    def test_the_exact_template_is_stale(self):
        assert qs._is_stale_fallback_cache(_template()) is True

    def test_order_does_not_matter(self):
        assert qs._is_stale_fallback_cache(list(reversed(_template()))) is True

    def test_a_real_ashby_schema_is_not(self):
        rows = [Row("name", "Full Name"), Row("email", "Email"), Row("resume", "Resume / CV"),
                Row("3ae71868-c7f3", "LinkedIn"), Row("5665ff15", "Are you available…")]
        assert qs._is_stale_fallback_cache(rows) is False

    def test_a_superset_is_a_real_form(self):
        """A Greenhouse form that asks the standard things plus one
        custom question was extracted, not guessed."""
        assert qs._is_stale_fallback_cache(_template() + [Row("question_1", "Why us?")]) is False

    def test_same_keys_different_labels_is_a_real_form(self):
        rows = _template()
        rows[0] = Row("first_name", "Given name")
        assert qs._is_stale_fallback_cache(rows) is False

    def test_empty_cache_is_not_stale(self):
        assert qs._is_stale_fallback_cache([]) is False

    def test_null_columns_are_tolerated(self):
        rows = _template()
        rows.append(Row(None, None))
        assert qs._is_stale_fallback_cache(rows) is False


class TestBothPathsUseIt:
    def test_async_path(self):
        src = inspect.getsource(qs.get_or_fetch_questions)
        assert "_is_stale_fallback_cache(cached)" in src
        assert "await db.delete(q)" in src

    def test_sync_path(self):
        src = inspect.getsource(qs.get_or_fetch_questions_sync)
        assert "_is_stale_fallback_cache(cached)" in src
        assert "session.delete(q)" in src
