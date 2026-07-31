"""F357 — AI model IDs come from one central constant, not stale literals.

Root cause of every AI feature being silently broken: the retired
model ``claude-sonnet-4-20250514`` (and ``claude-opus-4-7`` for cover
letters) was hardcoded in each feature. The live API returns 404 for
retired models, so resume customization / cover letters / interview
prep / insights all failed — insights most visibly (empty page).

These guards ensure the fix holds: the model IDs live in
``app.ai_models`` and no feature module reintroduces a bare literal.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("DATABASE_URL_SYNC", "postgresql://x:x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-ai-model")


def test_central_constants_present():
    from app.ai_models import CLAUDE_OPUS, CLAUDE_SONNET

    assert CLAUDE_SONNET == "claude-sonnet-4-6"
    assert CLAUDE_OPUS == "claude-opus-4-8"


def test_insights_model_version_uses_constant():
    from app.ai_models import CLAUDE_SONNET
    from app.workers.tasks._ai_insights import MODEL_VERSION

    assert MODEL_VERSION == CLAUDE_SONNET


def test_no_retired_model_literals_in_ai_call_sites():
    """No feature module may hardcode the retired model IDs."""
    backend = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for py in backend.rglob("*.py"):
        if py.name == "ai_models.py":  # doc-comment mentions them
            continue
        text = py.read_text()
        for bad in ("claude-sonnet-4-20250514", "claude-opus-4-7"):
            # Ignore doc-comment mentions (``e.g. ...``); only flag
            # lines that look like a model= / model_version= assignment.
            for line in text.splitlines():
                if bad in line and ("model" in line.lower()) and "=" in line and '"""' not in line and "e.g." not in line and "#" != line.strip()[:1]:
                    offenders.append(f"{py.name}: {line.strip()[:80]}")
    assert not offenders, "Retired model literal in a call site:\n" + "\n".join(offenders)
