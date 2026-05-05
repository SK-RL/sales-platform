"""F329 — surface AI-call failures from gen-recruitment-email.

Pre-fix the ``POST /companies/gen-recruitment-email`` handler
caught EVERY exception from the Anthropic API call and returned
HTTP 200 with the deterministic template body labelled
``generated_by: "template"`` — the SAME shape the no-key path
returns. The frontend rendered the template as if the AI had
produced it, the admin believed the email was AI-personalised,
and the recipient got a generic form letter.

F329 keeps the 200 + template body (UX continuity — the admin
should always have SOMETHING to send) but adds:

  * ``generated_by: "template_fallback"`` — distinct discriminator
    so observability + the frontend can tell "key not configured"
    apart from "key configured but call failed".
  * ``error: True`` flag — frontend uses this to render a clear
    "AI failed, edit before sending" warning.
  * ``error_message: str`` — human-readable fallback notice.

The handler also logs the exception class name at WARNING level
so admin can see the failure class (RateLimitError, APITimeoutError,
etc.) without leaking the API key or upstream stack to the client.

Same shape as F203 (resume.py customize) — surface the failure
without breaking the request/response contract.
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
os.environ.setdefault("JWT_SECRET", "pytest-f329")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "companies.py").read_text()


def test_handler_no_longer_uses_bare_template_label_on_error_path():
    """The except branch must NOT return ``generated_by: "template"``
    — that label is reserved for the no-key path. Returning the
    same string on the error path was the F329 root cause.
    """
    src = _read_handler()
    # Anchor on the gen-recruitment-email handler.
    handler_idx = src.find("async def draft_contact_email")
    assert handler_idx > 0, "draft_contact_email handler structure changed"
    # Look in the next ~3KB for the except branch.
    window = src[handler_idx:handler_idx + 8000]
    assert "except Exception" in window, "F329: error path removed entirely"
    # The except branch must not produce the ambiguous label.
    # We look for the specific shape that was the bug:
    # ``"generated_by": "template"`` IMMEDIATELY inside the except.
    except_idx = window.find("except Exception")
    except_window = window[except_idx:except_idx + 3500]
    assert '"generated_by": "template"' not in except_window, (
        "F329 regression: error-path response uses the no-key label "
        '``generated_by: "template"`` — the frontend can no longer '
        "distinguish key-not-configured from AI-call-failed."
    )


def test_handler_error_path_returns_distinct_discriminator():
    """The error path returns ``generated_by: "template_fallback"``
    — distinct from the no-key ``"template"`` label.
    """
    src = _read_handler()
    handler_idx = src.find("async def draft_contact_email")
    assert handler_idx > 0, "draft_contact_email handler structure changed"
    window = src[handler_idx:handler_idx + 8000]
    except_idx = window.find("except Exception")
    except_window = window[except_idx:except_idx + 3500]
    assert '"template_fallback"' in except_window, (
        "F329 regression: error-path discriminator missing. The "
        "frontend can't tell key-not-configured from AI-failed."
    )


def test_handler_error_path_sets_error_flag():
    """Frontend gates the warning banner on ``error: True``."""
    src = _read_handler()
    handler_idx = src.find("async def draft_contact_email")
    assert handler_idx > 0, "draft_contact_email handler structure changed"
    window = src[handler_idx:handler_idx + 8000]
    except_idx = window.find("except Exception")
    except_window = window[except_idx:except_idx + 3500]
    assert '"error": True' in except_window, (
        "F329 regression: error path no longer sets ``error: True`` "
        "— frontend can't render the failure warning."
    )


def test_handler_error_path_provides_actionable_message():
    """The fallback message must tell the admin to EDIT before
    sending, not just "AI failed". Otherwise the admin could send
    the generic template thinking it's AI-customised.
    """
    src = _read_handler()
    handler_idx = src.find("async def draft_contact_email")
    assert handler_idx > 0, "draft_contact_email handler structure changed"
    window = src[handler_idx:handler_idx + 8000]
    except_idx = window.find("except Exception")
    except_window = window[except_idx:except_idx + 3500]
    assert "error_message" in except_window, (
        "F329 regression: ``error_message`` field missing from error "
        "response — admin sees no warning."
    )
    # Look for the "edit before sending" kind of message.
    assert "edit" in except_window.lower(), (
        "F329 regression: fallback message no longer instructs the "
        "admin to edit before sending."
    )


def test_handler_logs_exception_class_on_failure():
    """Operators need to see the failure class in logs to tune
    rate-limits or retry timing. The error-path branch must log
    the exception class name.
    """
    src = _read_handler()
    handler_idx = src.find("async def draft_contact_email")
    assert handler_idx > 0, "draft_contact_email handler structure changed"
    window = src[handler_idx:handler_idx + 8000]
    except_idx = window.find("except Exception")
    except_window = window[except_idx:except_idx + 3500]
    assert "logging" in except_window, (
        "F329 regression: error path no longer logs the failure. "
        "Operators flying blind on AI outages."
    )
    assert "__class__.__name__" in except_window, (
        "F329 regression: log line no longer surfaces the exception "
        "class — admin can't tell rate-limit from timeout from "
        "content-block."
    )


def test_no_key_path_still_uses_plain_template_label():
    """The original no-key path (``ANTHROPIC_API_KEY`` not set)
    must keep returning ``generated_by: "template"`` so observability
    can still distinguish "intentionally degraded — admin hasn't
    configured AI" from "key configured but request failed".
    """
    src = _read_handler()
    # The no-key branch is the if-not-key check ABOVE the try block.
    handler_idx = src.find("async def draft_contact_email")
    assert handler_idx > 0, "draft_contact_email handler structure changed"
    window = src[handler_idx:handler_idx + 8000]
    no_key_idx = window.find("if not settings.anthropic_api_key")
    assert no_key_idx > 0, "no-key check structure changed"
    no_key_window = window[no_key_idx:no_key_idx + 500]
    assert '"generated_by": "template"' in no_key_window, (
        "F329 regression: the legitimate no-key path no longer "
        "labels its response — observability can't distinguish "
        "intentional degradation from AI failure."
    )
