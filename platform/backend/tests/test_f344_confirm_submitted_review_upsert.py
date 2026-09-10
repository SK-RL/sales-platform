"""F344 — confirm-submitted 500'd when re-applying to a reviewed job.

Reproduced live while applying to TensorWave "DevOps Engineer - AWS":
the Ashby form submitted successfully ("Your application was
successfully submitted"), and then
``POST /applications/{id}/confirm-submitted`` returned a bare 500. The
platform recorded nothing — no submission row, no applied status, no
pipeline card — for an application that had genuinely been sent.

Root cause: step 5b blind-INSERTed a Review, under a comment asserting
"reviews is an event log, not per-(job,user) state". That assertion was
invalidated by F281, which added::

    CREATE UNIQUE INDEX uq_reviews_job_reviewer ON reviews (job_id, reviewer_id)

So every job the user had *already reviewed* raised IntegrityError. That
is not an edge case: rejecting a role and later re-applying is routine,
and it was guaranteed for the batch of previously-rejected jobs that had
just been reopened.

The ordering is what makes it severe — the ATS submit happens BEFORE
this call, so the failure silently desynchronised the platform from
reality on exactly the applications the user most wanted tracked.

F344:
  * upserts the Review (applying supersedes any earlier decision),
  * guards the ``job.status = "accepted"`` flip with F343's
    ``_active_duplicates`` since "accepted" is covered by F316's
    partial UNIQUE too, downgrading a collision to a reported issue
    rather than a lost submission,
  * catches IntegrityError at commit and returns a 409 that says the
    ATS submit succeeded, so a bare 500 can never again imply nothing
    was sent.
"""
from __future__ import annotations

import os
import pathlib

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f344")

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SRC = (_BACKEND / "app" / "api" / "v1" / "applications.py").read_text()

# The exact slice of confirm_submitted this regression lives in.
_START = _SRC.index("async def confirm_submitted(")
_END = _SRC.index("def _sanitize_payload_for_storage(")
_HANDLER = _SRC[_START:_END]


def test_the_unique_index_this_regression_depends_on_still_exists():
    """If F281's index is ever dropped, this test should be revisited
    rather than silently passing for the wrong reason."""
    migration = (
        _BACKEND / "alembic" / "versions"
        / "2026_04_29_k7l8m9n0o1p2_reviews_unique_job_reviewer.py"
    ).read_text()
    assert "CREATE UNIQUE INDEX uq_reviews_job_reviewer " in migration
    assert "ON reviews (job_id, reviewer_id)" in migration


def test_review_is_looked_up_before_insert():
    """The blind INSERT is gone; the handler selects first."""
    assert "Review.job_id == app.job_id," in _HANDLER
    assert "Review.reviewer_id == user.id," in _HANDLER
    assert "if review is None:" in _HANDLER


def test_existing_review_is_updated_not_duplicated():
    assert 'review.decision = "accepted"' in _HANDLER
    assert "superseded earlier decision" in _HANDLER


def test_the_false_event_log_comment_is_gone():
    """That comment is what made the blind INSERT look correct."""
    assert "reviews table is an event log" not in _HANDLER


def test_job_status_flip_is_guarded_by_the_jobs_dedupe_check():
    """"accepted" is an ACTIVE status, so it is gated by F316 too."""
    assert '_active_duplicates([job], "accepted", db)' in _HANDLER
    assert "job_status_not_advanced" in _HANDLER
    # The flip must be in the else branch, never unconditional.
    assert 'job.status = "accepted"' in _HANDLER
    guarded = _HANDLER.index('_active_duplicates([job], "accepted", db)')
    flip = _HANDLER.index('job.status = "accepted"')
    assert flip > guarded, "the status flip must sit behind the guard"


def test_commit_failure_says_the_ats_submit_already_happened():
    """A bare 500 reads as 'nothing happened'. It did happen."""
    assert "except IntegrityError:" in _HANDLER
    assert "status_code=409" in _HANDLER
    assert "was submitted to the ATS but could not be" in _HANDLER


def test_active_duplicates_is_importable_from_jobs():
    """The cross-module import must actually resolve."""
    from app.api.v1.jobs import _active_duplicates  # noqa: F401
