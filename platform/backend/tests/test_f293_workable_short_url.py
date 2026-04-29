"""F293 — Workable short-form URL targeted error message (closes F229).

F229 found ``POST /jobs/submit-link`` rejected EVERY Workable URL
the user could copy out of our own dashboard. Root cause: the
parser regex required ``apply.workable.com/{slug}/j/{id}`` (3 path
segments) but the Workable scanner reads ``url`` /
``application_url`` directly off the API response, which always
supplies the 2-segment ``apply.workable.com/j/{id}`` short form.
100% of Workable rows in the DB were short-form at F229
verification time.

Short-form URLs can't be ingested directly because the Workable
widget API the fetcher uses is keyed on company-slug — we can't
look up the company from a bare job-id. F293's targeted fix is
to detect the short-form pattern after the main loop fails and
raise a SPECIFIC error message with actionable guidance ("use
the long-form URL or ask admin to add the board") instead of
the misleading generic "URL host is not a recognized ATS" that
hid which platform was actually involved.

Long-form + legacy subdomain Workable URLs continue to parse
unchanged.
"""
from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f293")


def test_long_form_workable_url_still_works():
    """The 3-segment long form must still parse — F293 only adds
    a more-specific error path for the short form, it must not
    break the working long form.
    """
    from app.fetchers.url_parser import parse_job_url

    parsed = parse_job_url(
        "https://apply.workable.com/companyslug/j/ABCDEF1234"
    )
    assert parsed.platform == "workable"
    assert parsed.slug == "companyslug"
    assert parsed.external_id == "ABCDEF1234"


def test_legacy_subdomain_workable_url_still_works():
    """Legacy ``{slug}.workable.com/jobs/{id}`` continues to parse."""
    from app.fetchers.url_parser import parse_job_url

    parsed = parse_job_url("https://acme.workable.com/jobs/12345")
    assert parsed.platform == "workable"
    assert parsed.slug == "acme"


def test_short_form_url_raises_targeted_error():
    """Short-form URLs raise ``UnsupportedJobUrlError`` with a
    detail that mentions Workable specifically + suggests the
    long-form URL — the F229 user-visible bug was the GENERIC
    "not a recognized ATS" message, which hid that Workable IS
    supported.
    """
    from app.fetchers.url_parser import parse_job_url, UnsupportedJobUrlError
    import pytest

    with pytest.raises(UnsupportedJobUrlError) as exc_info:
        parse_job_url("https://apply.workable.com/j/ABCDEF1234")
    msg = str(exc_info.value)
    # Targeted detail must mention Workable + the long-form shape
    # so the user knows what to do.
    assert "Workable" in msg, (
        "F293 regression: short-form URL error message no longer "
        "mentions Workable. User gets the generic "
        "'not a recognized ATS' message back, which is misleading "
        "because Workable IS in the supported list."
    )
    assert "long-form" in msg.lower() or "company-slug" in msg.lower(), (
        "F293 regression: short-form URL error message no longer "
        "guides the user toward the long-form URL fix."
    )


def test_short_form_url_does_not_match_generic_error():
    """The short-form URL must NOT fall through to the generic
    'URL host is not a recognized ATS' error. That was the
    misleading message F229 flagged.
    """
    from app.fetchers.url_parser import parse_job_url, UnsupportedJobUrlError
    import pytest

    with pytest.raises(UnsupportedJobUrlError) as exc_info:
        parse_job_url("https://apply.workable.com/j/SHORT1")
    msg = str(exc_info.value)
    assert "not a recognized ATS" not in msg, (
        "F293 regression: short-form URL is back to the generic "
        "'not a recognized ATS' error message. Users see Workable "
        "in the supported list but get told their URL isn't from "
        "a supported ATS — the exact circular UX failure F229 "
        "documented."
    )


def test_genuine_unsupported_url_still_falls_through_to_generic():
    """The targeted Workable short-form path must not capture
    OTHER unsupported URLs — they should still get the generic
    error so the user knows the host isn't supported at all.
    """
    from app.fetchers.url_parser import parse_job_url, UnsupportedJobUrlError
    import pytest

    with pytest.raises(UnsupportedJobUrlError) as exc_info:
        parse_job_url("https://www.linkedin.com/jobs/view/12345")
    msg = str(exc_info.value)
    assert "not a recognized ATS" in msg, (
        "F293 regression: unrelated unsupported URLs no longer hit "
        "the generic error path. The Workable-specific message "
        "was meant ONLY for short-form Workable URLs."
    )
