"""F363 — alternative groups vanished on every cached read.

F350 made the gate treat Greenhouse's resume | resume_text as one
requirement via a shared ``alternative_group`` tag set by the extractor.
``job_questions`` has no such column, so the tag survived only the first
fetch; every cached read after that — nearly every view in production —
came back untagged and ``resume_text`` blocked on its own even with a
resume attached. Seen live: a review screen still listing "Resume/CV" as
blocking after F362 should have cleared it.

Alternatives are recoverable from the cache: they are the rows of one
job that share a label.
"""
from app.services.question_service import _cached_row_to_dict, _with_alternative_groups
from app.workers.tasks._answer_prep import blocking_gaps, match_questions_to_answers


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _row(field_key, label, field_type, required=True, platform="greenhouse"):
    return Row(field_key=field_key, label=label, field_type=field_type, required=required,
               options=[], description="", platform=platform)


CACHED = [
    _row("resume", "Resume/CV", "file"),
    _row("resume_text", "Resume/CV", "textarea"),
    _row("first_name", "First Name", "text"),
]


class TestDerivation:
    def test_rows_sharing_a_label_get_one_group(self):
        rows = _with_alternative_groups([_cached_row_to_dict(r) for r in CACHED])
        by = {r["field_key"]: r for r in rows}
        assert by["resume"]["alternative_group"]
        assert by["resume"]["alternative_group"] == by["resume_text"]["alternative_group"]

    def test_a_unique_label_stays_ungrouped(self):
        rows = _with_alternative_groups([_cached_row_to_dict(r) for r in CACHED])
        assert next(r for r in rows if r["field_key"] == "first_name")["alternative_group"] == ""

    def test_matches_the_extractor_naming(self):
        rows = _with_alternative_groups([_cached_row_to_dict(r) for r in CACHED])
        assert next(r for r in rows if r["field_key"] == "resume")["alternative_group"] == "altgroup_resumecv"


class TestGateBehaviourFromCache:
    def test_attached_resume_clears_both_alternatives(self):
        """The live regression: cached rows, resume attached, and
        Resume/CV still blocking."""
        rows = _with_alternative_groups([_cached_row_to_dict(r) for r in CACHED])
        matched = match_questions_to_answers(
            rows, [{"question_key": "first_name", "answer": "Sarthak", "category": "", "source": "manual"}]
        )
        assert blocking_gaps(matched, satisfied_field_keys={"resume"}) == []

    def test_without_the_derivation_it_would_block(self):
        """Documents why the derivation exists."""
        rows = [_cached_row_to_dict(r) for r in CACHED]  # untagged, as before F363
        matched = match_questions_to_answers(
            rows, [{"question_key": "first_name", "answer": "Sarthak", "category": "", "source": "manual"}]
        )
        gaps = blocking_gaps(matched, satisfied_field_keys={"resume"})
        assert [g["field_key"] for g in gaps] == ["resume_text"]
