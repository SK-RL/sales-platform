"""F288 — StageCreate/StageUpdate cap + HTML strip (closes F149 b).

F149(b) found that ``StageCreate.label`` accepted unsanitized HTML,
so an admin could create a Kanban stage with
``label="<img src=x onerror=alert(1)>"`` and the payload would be
persisted + rendered verbatim in the Pipeline Kanban column header
(stored XSS).

F288 caps ``key`` at 50 chars + slug-pattern, ``label`` at 100
chars + ``strip_html_tags``, ``color`` at 60 chars. The PATCH
path (``StageUpdate``) gets the same field validator so it can't
be a parallel vector.
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
os.environ.setdefault("JWT_SECRET", "pytest-f288")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_stage_create_label_strips_html_via_validator():
    """The schema's ``label`` field must route through
    ``strip_html_tags`` so HTML payloads get neutralised at parse
    time before any DB write."""
    from app.api.v1.pipeline import StageCreate

    s = StageCreate(
        key="custom_stage",
        label="<img src=x onerror=alert(1)>Important",
    )
    assert "<img" not in s.label, (
        "F288 regression: StageCreate no longer strips HTML from "
        "``label``. Stored XSS via Kanban column header reopens."
    )
    assert "alert" not in s.label, (
        "F288 regression: StageCreate strip_html_tags didn't drop "
        "the script payload from the label."
    )


def test_stage_update_label_strips_html_via_validator():
    """The PATCH path was a parallel vector pre-fix — admin
    could create a clean stage then PATCH the label to a payload.
    """
    from app.api.v1.pipeline import StageUpdate

    s = StageUpdate(label="<script>alert(1)</script>Renamed")
    assert "<script>" not in (s.label or "")
    assert "alert" not in (s.label or "")


def test_stage_create_caps_key_length():
    """Key cap at 50 chars."""
    from app.api.v1.pipeline import StageCreate
    import pydantic

    long_key = "a" * 51
    try:
        StageCreate(key=long_key, label="ok")
    except pydantic.ValidationError:
        return  # expected
    raise AssertionError(
        "F288 regression: StageCreate.key length cap (50 chars) "
        "is gone. 50KB keys would land in the column writer."
    )


def test_stage_create_key_pattern_rejects_html():
    """Key pattern is slug-safe — no HTML characters allowed."""
    from app.api.v1.pipeline import StageCreate
    import pydantic

    try:
        StageCreate(key="<script>", label="ok")
    except pydantic.ValidationError:
        return
    raise AssertionError(
        "F288 regression: StageCreate.key no longer enforces a "
        "slug-safe pattern. ``<script>`` could become a stage key."
    )


def test_stage_create_label_caps_length():
    """Label cap at 100 chars."""
    from app.api.v1.pipeline import StageCreate
    import pydantic

    long_label = "x" * 101
    try:
        StageCreate(key="ok", label=long_label)
    except pydantic.ValidationError:
        return
    raise AssertionError(
        "F288 regression: StageCreate.label length cap (100 chars) "
        "is gone."
    )


def test_extra_forbid_still_in_place():
    """Defense-in-depth — F268 ``extra=\"forbid\"`` must still
    be on both schemas. F288 didn't touch it but a future refactor
    that re-orders ``model_config`` could drop it accidentally.
    """
    from app.api.v1.pipeline import StageCreate, StageUpdate

    cfg_create = getattr(StageCreate, "model_config", {})
    cfg_update = getattr(StageUpdate, "model_config", {})
    assert cfg_create.get("extra") == "forbid", (
        "F288 regression: StageCreate ``extra=\"forbid\"`` was removed."
    )
    assert cfg_update.get("extra") == "forbid", (
        "F288 regression: StageUpdate ``extra=\"forbid\"`` was removed."
    )
