"""F395 — every AI feature goes through app.ai_client on claude-opus-5.

What the helper guards, each a real failure mode on this model:
thinking blocks ahead of the text (content[0].text is not the answer),
max_tokens that must cover thinking, and the refusal stop reason.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai_client import AIRefused, complete, text_of


class _Client:
    def __init__(self, message):
        self.message = message
        self.kwargs = None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return self.message


def _msg(blocks, stop_reason="end_turn", details=None):
    return SimpleNamespace(content=[SimpleNamespace(**b) for b in blocks], stop_reason=stop_reason, stop_details=details)


def test_text_skips_thinking_blocks():
    m = _msg([{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}])
    assert text_of(m) == "Hello world"


def test_request_shape_on_opus_5():
    c = _Client(_msg([{"type": "text", "text": "ok"}]))
    text, _ = complete("hi", system="sys", answer_tokens=600, client=c)
    assert text == "ok"
    k = c.kwargs
    assert k["model"] == "claude-opus-5" and k["system"] == "sys"
    assert k["max_tokens"] > 600  # headroom for thinking
    assert k["output_config"] == {"effort": "medium"}
    assert k["extra_body"] == {"fallbacks": "default"} and "server-side-fallback" in k["extra_headers"]["anthropic-beta"]
    assert "temperature" not in k and "thinking" not in k  # rejected / default adaptive


def test_refusal_raises_instead_of_index_error():
    c = _Client(_msg([], stop_reason="refusal", details=SimpleNamespace(category="cyber", explanation="no")))
    with pytest.raises(AIRefused) as exc:
        complete("hi", client=c)
    assert exc.value.category == "cyber"


def test_no_feature_calls_the_sdk_directly():
    backend = Path(__file__).resolve().parent.parent / "app"
    offenders = [str(p.relative_to(backend)) for p in backend.rglob("*.py")
                 if p.name not in ("ai_client.py", "ai_models.py") and ("messages.create(" in p.read_text() or "content[0].text" in p.read_text())]
    assert offenders == []
