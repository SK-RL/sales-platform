"""F289 — CompanyContact name/title HTML strip + outreach_status
Literal (closes F148 a+c).

F148 found two issues on the contact schemas:
  (a) ``first_name`` / ``last_name`` / ``title`` / ``department``
      had length caps but no HTML sanitization, so an admin could
      persist ``first_name="<script>alert(1)</script>"`` and the
      payload would render verbatim in the admin contact UI.
  (c) ``CompanyContactUpdate.outreach_status`` had ``max_length=50``
      but NO ``Literal`` constraint, so a generic
      ``PATCH /companies/{cid}/contacts/{id}`` could persist
      arbitrary values like ``"hacked_status_zzz"`` (the dedicated
      ``/outreach`` endpoint enforces ``_VALID_OUTREACH`` in the
      handler but the generic PATCH bypassed that helper).

F289 ships:
  * ``_strip_html`` validator on first_name/last_name/title/department
    in both ``CompanyContactCreate`` and ``CompanyContactUpdate``.
  * ``Literal`` typing on ``outreach_status`` in
    ``CompanyContactUpdate`` and ``OutreachUpdate`` so any
    non-canonical value 422s at parse time.
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
os.environ.setdefault("JWT_SECRET", "pytest-f289")


def test_create_strips_html_in_name_fields():
    """``CompanyContactCreate`` must drop HTML from
    first_name/last_name/title/department.
    """
    from app.schemas.company_contact import CompanyContactCreate

    c = CompanyContactCreate(
        first_name="<script>alert(1)</script>John",
        last_name="<img src=x onerror=alert(1)>Doe",
        title="<svg onload=alert(1)>Director",
        department="<a href='javascript:'>Eng</a>",
        email="john@example.com",
    )
    for field, value in (
        ("first_name", c.first_name),
        ("last_name", c.last_name),
        ("title", c.title),
        ("department", c.department),
    ):
        assert "<" not in value, (
            f"F289 regression: CompanyContactCreate no longer strips "
            f"HTML from ``{field}``. Stored XSS in admin UI reopens. "
            f"Got: {value!r}"
        )
        assert "alert" not in value, (
            f"F289 regression: ``{field}`` still contains the script "
            f"payload. strip_html_tags isn't running. Got: {value!r}"
        )


def test_update_strips_html_in_name_fields():
    """``CompanyContactUpdate`` PATCH path must apply the same
    strip — pre-fix you could create a clean contact and PATCH a
    payload in. Note: ``<script>`` tags AND their contents are
    dropped entirely (text inside a script is JS, not display
    text), so the result for a pure-payload input is empty string;
    that's correct behaviour. The security guarantee is "no HTML
    tags survive", not "result is non-empty".
    """
    from app.schemas.company_contact import CompanyContactUpdate

    u = CompanyContactUpdate(
        first_name="<script>alert(1)</script>JohnPATCH",
        title="<img src=x onerror=alert(1)> Director",
    )
    # Tags removed; the text suffix outside the script tag survives.
    assert u.first_name == "JohnPATCH", (
        f"F289 regression: PATCH-side first_name strip failed. "
        f"Got: {u.first_name!r}"
    )
    # ``<img>`` is a self-closing tag; its attributes (alert(1))
    # don't render as visible text. The trailing " Director" is
    # text outside the tag and survives.
    assert u.title and "<" not in u.title and "alert" not in u.title
    # Pure-payload (no surrounding text) collapses to empty —
    # acceptable; it's the strongest possible XSS defense.
    u2 = CompanyContactUpdate(first_name="<script>x</script>")
    assert "<" not in (u2.first_name or "")


def test_update_outreach_status_rejects_non_canonical():
    """``CompanyContactUpdate.outreach_status`` must be a
    Literal — non-canonical values 422 at parse time instead of
    sailing through and clobbering the dedicated /outreach
    endpoint's ``_VALID_OUTREACH`` check.
    """
    from app.schemas.company_contact import CompanyContactUpdate
    import pydantic

    # Canonical values pass.
    for status in (
        "not_contacted", "emailed", "replied", "meeting_scheduled", "not_interested",
    ):
        u = CompanyContactUpdate(outreach_status=status)
        assert u.outreach_status == status

    # Non-canonical values 422 (Pydantic raises ValidationError).
    for bad in ("hacked_status_zzz", "ARBITRARY", "", "  emailed  "):
        try:
            CompanyContactUpdate(outreach_status=bad)
        except pydantic.ValidationError:
            continue
        raise AssertionError(
            f"F289 regression: CompanyContactUpdate.outreach_status "
            f"accepted non-canonical value {bad!r}. The Literal "
            f"constraint is gone — generic PATCH can smuggle "
            f"arbitrary status strings."
        )


def test_outreach_update_status_rejects_non_canonical():
    """``OutreachUpdate.outreach_status`` (the dedicated endpoint)
    must also be Literal-typed for defense in depth alongside
    the handler's ``_VALID_OUTREACH`` check.
    """
    from app.schemas.company_contact import OutreachUpdate
    import pydantic

    OutreachUpdate(outreach_status="emailed")
    try:
        OutreachUpdate(outreach_status="bogus")
    except pydantic.ValidationError:
        return
    raise AssertionError(
        "F289 regression: OutreachUpdate.outreach_status no longer "
        "rejects non-canonical values at parse time."
    )


def test_strip_html_passes_through_safe_strings():
    """Normal name strings round-trip unchanged. Otherwise the
    validator would mutate every legit name and confuse the UI.
    """
    from app.schemas.company_contact import CompanyContactCreate

    c = CompanyContactCreate(
        first_name="Carolyn",
        last_name="O'Brien",
        title="Engineering Manager",
        department="Platform Engineering",
        email="c@example.com",
    )
    assert c.first_name == "Carolyn"
    assert c.last_name == "O'Brien"
    assert c.title == "Engineering Manager"
    assert c.department == "Platform Engineering"
