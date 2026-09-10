"""F348 — Recruitee application-question extraction.

Recruitee's public offers API returns the real custom questions for a
posting, so we can stop guessing its forms. The fixture below is the
literal shape returned by a live board (multiplier.recruitee.com, offer
2697445) — trimmed, not invented — so this pins the mapping against what
the ATS actually sends rather than against documentation.

Why this matters beyond one platform: the apply gate refuses to
auto-submit any posting whose schema is `extraction_mode="fallback"`.
Every extractor added here converts a whole platform from "needs a human"
to "auto-applyable", which is the only lever that moves apply volume.
"""

import httpx
import pytest

from app.fetchers.questions import (
    SUPPORTED_QUESTION_PLATFORMS,
    _fetch_recruitee_questions,
    _recruitee_offer_id,
    fetch_application_questions,
)

# Trimmed from the live response. `kind` values observed in the wild:
# text, string, multi_choice, boolean, video.
LIVE_OFFER = {
    "offer": {
        "id": 2697445,
        "title": "Marketing Lead",
        "open_questions": [
            {
                "id": 4281785,
                "kind": "text",
                "required": True,
                "body": "What do you consider to be your top 5 core strengths?",
                "open_question_options": [],
            },
            {
                "id": 4281786,
                "kind": "multi_choice",
                "required": True,
                "body": "Which areas you feel are your strongest:",
                "open_question_options": [
                    {"id": 6588542, "body": "Market research"},
                    {"id": 6588541, "body": "Content strategy"},
                    {"id": 6588540, "body": "SEO / SEM"},
                ],
            },
            {
                "id": 4281787,
                "kind": "boolean",
                "required": True,
                "body": "Are you ready to be a mentor for less experienced marketers?",
                "open_question_options": [],
            },
            {
                "id": 4281788,
                "kind": "string",
                "required": False,
                "body": "Which city do you live in?",
                "open_question_options": [],
            },
            {
                "id": 4281789,
                "kind": "video",
                "required": False,
                "body": "Tell us why you want to join us.",
                "open_question_options": [],
            },
        ],
    }
}


@pytest.fixture
def stub_recruitee(monkeypatch):
    """Serve LIVE_OFFER for any Recruitee offer URL."""
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return LIVE_OFFER

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            captured["url"] = url
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    return captured


class TestOfferIdParsing:
    def test_strips_our_prefix(self):
        """fetchers/recruitee.py stores f"recruitee-{id}" so the global
        UNIQUE on jobs.external_id can't collide with another ATS."""
        assert _recruitee_offer_id("recruitee-2697445") == "2697445"

    def test_bare_id_passes_through(self):
        assert _recruitee_offer_id("2697445") == "2697445"

    def test_whitespace_tolerated(self):
        assert _recruitee_offer_id("  recruitee-99  ") == "99"

    def test_empty_is_empty(self):
        assert _recruitee_offer_id("") == ""


class TestExtraction:
    def test_hits_the_right_endpoint(self, stub_recruitee):
        _fetch_recruitee_questions("recruitee-2697445", "multiplier")
        assert stub_recruitee["url"] == (
            "https://multiplier.recruitee.com/api/offers/2697445"
        )

    def test_returns_fixed_fields_plus_custom_questions(self, stub_recruitee):
        fields = _fetch_recruitee_questions("recruitee-2697445", "multiplier")
        keys = [f["field_key"] for f in fields]
        # Recruitee renders the identity block on every offer.
        for fixed in ("name", "email", "phone", "resume", "cover_letter"):
            assert fixed in keys, fixed
        assert "open_question_4281786" in keys

    def test_kind_mapping(self, stub_recruitee):
        by_key = {
            f["field_key"]: f
            for f in _fetch_recruitee_questions("recruitee-2697445", "multiplier")
        }
        assert by_key["open_question_4281785"]["field_type"] == "textarea"  # text
        assert by_key["open_question_4281788"]["field_type"] == "text"      # string
        assert by_key["open_question_4281786"]["field_type"] == "select"
        assert by_key["open_question_4281787"]["field_type"] == "boolean"

    def test_video_question_is_typed_as_file(self, stub_recruitee):
        """A video answer can't be produced unattended. Typing it as a
        file means the gate leaves it unanswered — and blocks if it's
        required, which is the correct outcome."""
        by_key = {
            f["field_key"]: f
            for f in _fetch_recruitee_questions("recruitee-2697445", "multiplier")
        }
        assert by_key["open_question_4281789"]["field_type"] == "file"

    def test_options_are_flattened_to_labels(self, stub_recruitee):
        by_key = {
            f["field_key"]: f
            for f in _fetch_recruitee_questions("recruitee-2697445", "multiplier")
        }
        assert by_key["open_question_4281786"]["options"] == [
            "Market research",
            "Content strategy",
            "SEO / SEM",
        ]

    def test_required_flag_is_carried(self, stub_recruitee):
        by_key = {
            f["field_key"]: f
            for f in _fetch_recruitee_questions("recruitee-2697445", "multiplier")
        }
        assert by_key["open_question_4281785"]["required"] is True
        assert by_key["open_question_4281788"]["required"] is False

    def test_malformed_questions_are_skipped_not_fatal(self, monkeypatch):
        broken = {
            "offer": {
                "open_questions": [
                    {"id": None, "body": "no id", "kind": "text"},
                    {"id": 5, "body": "", "kind": "text"},
                    "not-a-dict",
                    {"id": 7, "body": "kept", "kind": "text"},
                ]
            }
        }

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return broken

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                return _Resp()

        monkeypatch.setattr(httpx, "Client", _Client)
        keys = [
            f["field_key"]
            for f in _fetch_recruitee_questions("recruitee-1", "acme")
        ]
        assert "open_question_7" in keys
        assert "open_question_5" not in keys

    def test_missing_slug_returns_nothing(self):
        """No slug means no URL we could call. Returning [] makes
        fetch_application_questions fall back and mark it as guessed,
        rather than raising."""
        assert _fetch_recruitee_questions("recruitee-1", "") == []


class TestWiring:
    def test_recruitee_is_now_supported(self):
        assert "recruitee" in SUPPORTED_QUESTION_PLATFORMS

    def test_dispatcher_marks_extracted(self, stub_recruitee):
        fields = fetch_application_questions("recruitee", "recruitee-2697445", "multiplier")
        assert fields
        assert all(f["extraction_mode"] == "extracted" for f in fields)
        assert not any("fallback_reason" in f for f in fields)

    def test_fetch_failure_falls_back_and_says_so(self, monkeypatch):
        class _Boom:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "Client", _Boom)
        fields = fetch_application_questions("recruitee", "recruitee-1", "acme")
        assert all(f["extraction_mode"] == "fallback" for f in fields)
        assert all(f["fallback_reason"] == "fetch_failed" for f in fields)
