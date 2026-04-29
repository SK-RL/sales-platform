"""Pydantic schemas for resume endpoints.

Regression finding 90: `POST /api/v1/resume/{id}/customize` declared
`body: dict`, so a caller POSTing `{"target_score": "high"}` crashed
the request with `TypeError: '<=' not supported between instances of
'int' and 'str'` — the handler did `if not (60 <= target_score <= 95)`
against whatever type came in. The error surfaced as a 500 with a
non-JSON stack trace, not the clean 400 the caller expected.

Same failure-mode class as findings #79 (credentials) and #80 (answer
book): `body: dict` writer endpoints can't enforce numeric bounds or
type coercion, so any numeric-looking field POSTed as a string crashes
with a 500 instead of returning a 422.

This schema pins the shape: `job_id` is a proper UUID (422 on garbage
strings), and `target_score` is an `int` bounded to [60, 95] via
Pydantic `Field(ge=..., le=...)` — so `"high"`, `-5`, `120`, `42.7`
and `null` all fail at parse time with a clean 422, never reach the
comparison, and never log a 500 stack trace.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Target-score bounds mirror the original manual guard in
# `api/v1/resume.py::customize_resume_for_job` (60 ≤ target ≤ 95). 60
# is the minimum useful bar for AI rewrite (below that the resume
# usually just needs the base scoring path); 95 is the ceiling we
# trust the model to hit without fabricating experience.
_TARGET_SCORE_MIN = 60
_TARGET_SCORE_MAX = 95
_TARGET_SCORE_DEFAULT = 85


class CustomizeRequest(BaseModel):
    """Body of POST /api/v1/resume/{resume_id}/customize.

    Pydantic enforces UUID parsing on `job_id` and an int-coercible
    value in [60, 95] on `target_score`. String `"85"` is accepted
    (Pydantic coerces); string `"high"` is rejected with a 422.

    Regression finding 231: `extra="forbid"` is the missing parity fix
    — `CoverLetterRequest` and `InterviewPrepRequest` got it via F157
    but this schema was overlooked. Without it, a frontend typo on
    `target_score` (e.g. `targetScore`, `target_Score`, `targetscore`)
    silently falls back to the default 85, producing a different
    customization than the user asked for with no error to surface
    the mismatch. Forbidding unknowns turns silent drops into a clean
    422 with the offending field name in the detail.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    target_score: int = Field(
        default=_TARGET_SCORE_DEFAULT,
        ge=_TARGET_SCORE_MIN,
        le=_TARGET_SCORE_MAX,
        description="Target ATS score (60-95) the AI customization should aim for.",
    )


# Regression finding 132: `PATCH /resume/{resume_id}/label` used `body:
# dict`, so a caller POSTing `{"label": 12345}` or `{"label": ["a"]}`
# crashed the handler with `'int'/'list' object has no attribute
# 'strip'` → leaked 500 stack trace. Same failure class as F79/F80/F90
# — `body: dict` can't enforce type coercion. Pydantic model below
# catches every non-string payload (including `null`, numbers, arrays,
# nested dicts) at parse time with a clean 422, strips whitespace via
# `@field_validator` (pydantic-v2 doesn't expose `strip_whitespace` as a
# Field arg the way v1 did), and caps at 100 chars to match the
# `Resume.label` DB column's `String(100)` constraint — so a 500-char
# label from a rogue client also 422s instead of crashing the DB insert
# with an integrity error.
class ResumeLabelUpdate(BaseModel):
    """Body of PATCH /api/v1/resume/{resume_id}/label."""

    # F306c (sweep follow-up): ``extra="forbid"`` so a typoed
    # field like ``labl`` 422s instead of silently dropping.
    # Same defense pattern as every other *Create/*Update schema
    # post-F306.
    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Display label for the resume (1-100 chars, stripped).",
    )

    @field_validator("label", mode="before")
    @classmethod
    def _strip_and_guard(cls, v):
        # Run BEFORE min_length/max_length so whitespace-only inputs
        # collapse to empty string and fail the `min_length=1` check
        # with a readable "String should have at least 1 character"
        # 422, instead of going through the DB and getting the
        # original finding's `400 "Label cannot be empty"` message at
        # best or a 500 at worst. Also lets us reject non-string types
        # loudly (int/list/dict/null) with a clear message.
        if v is None:
            raise ValueError("label must not be null")
        if not isinstance(v, str):
            raise ValueError("label must be a string")
        return v.strip()
