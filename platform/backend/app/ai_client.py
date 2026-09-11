"""One way to call Claude from the platform.

F395 — every AI feature moved to ``claude-opus-5`` (Sarthak: "use opus 5
for everything for now"). Three things about that model differ from the
Sonnet/Haiku calls the features were written against, and this helper
is where they are handled once rather than seven times:

* **Thinking is on by default** and its tokens count against
  ``max_tokens`` — a tight cap sized around the answer now truncates
  mid-response. Callers pass the answer size they need; the helper adds
  headroom.
* **The first content block may be a thinking block**, so
  ``message.content[0].text`` is no longer the answer. The helper returns
  the joined text blocks.
* **Safety classifiers can decline** (HTTP 200, ``stop_reason:
  "refusal"``). The request asks for Anthropic's server-side fallback
  (``fallbacks: "default"``); if that still refuses, ``AIRefused`` is
  raised instead of an IndexError from an empty content list.

Effort defaults to ``medium``: on Claude Opus 5 the low/medium levels are
the primary cost lever and hold quality on short generation tasks.
"""

from __future__ import annotations

import logging
from typing import Any

from app.ai_models import CLAUDE_OPUS
from app.config import get_settings

logger = logging.getLogger(__name__)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Room for thinking on top of the answer the caller sized for.
_THINKING_HEADROOM = 4096


class AIUnavailable(RuntimeError):
    """No API key configured."""


class AIRefused(RuntimeError):
    """The model declined the request (safety classifier)."""

    def __init__(self, category: str | None, explanation: str | None):
        super().__init__(f"model refused ({category or 'unspecified'}): {explanation or ''}".strip())
        self.category = category
        self.explanation = explanation


def get_client():
    """The SDK client, or None when no key is configured."""
    settings = get_settings()
    raw = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if not raw.strip():
        return None
    import anthropic  # lazy: non-AI code paths don't pay the import

    return anthropic.Anthropic(api_key=raw)


def text_of(message: Any) -> str:
    """The answer text of a response, skipping thinking blocks."""
    parts = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def complete(
    prompt: str,
    *,
    system: str | None = None,
    answer_tokens: int = 4000,
    effort: str = "medium",
    model: str = CLAUDE_OPUS,
    client: Any = None,
) -> tuple[str, Any]:
    """One-shot completion. Returns ``(text, message)``.

    ``answer_tokens`` is the size of the answer the caller expects; the
    request's ``max_tokens`` adds headroom for thinking.
    """
    client = client or get_client()
    if client is None:
        raise AIUnavailable("ANTHROPIC_API_KEY is not configured")
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": int(answer_tokens) + _THINKING_HEADROOM,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": effort},
        "extra_headers": {"anthropic-beta": _FALLBACK_BETA},
        "extra_body": {"fallbacks": "default"},
    }
    if system:
        kwargs["system"] = system
    message = client.messages.create(**kwargs)
    if getattr(message, "stop_reason", None) == "refusal":
        details = getattr(message, "stop_details", None)
        raise AIRefused(getattr(details, "category", None), getattr(details, "explanation", None))
    text = text_of(message)
    if getattr(message, "stop_reason", None) == "max_tokens":
        logger.warning("ai_client: answer truncated at max_tokens (%s)", kwargs["max_tokens"])
    return text, message
