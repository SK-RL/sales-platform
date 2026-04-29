"""Pydantic schemas for platform credential CRUD.

Regression finding 77 (stored XSS) + 79 (body: dict API hygiene):
`POST /api/v1/credentials/{resume_id}` previously declared `body: dict`,
so `profile_url` was stored verbatim. A `javascript:` scheme survived
storage and, when the frontend rendered it as `<a href={…}>`, any
subsequent click executed JS in the viewer's session. `rel=noopener`
does NOT block `javascript:` execution.

This schema closes the gap: `profile_url` is scheme-checked (http/https/
relative only), and `platform`, `email`, `password` get proper typing
so a malformed payload fails fast at request parse time with a 422
instead of crashing a `.strip()`/`.lower()` call later.

The URL-scheme validator mirrors the private helper in
`schemas/feedback.py` (`_validate_optional_url`). Kept local rather
than cross-imported — identical logic, zero runtime coupling between
unrelated schema modules.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# Supported credential platforms — must stay in sync with
# `api/v1/credentials.py::SUPPORTED_PLATFORMS` and the frontend
# `CredentialsPage.tsx::PLATFORM_LABELS`. If a new fetcher is added,
# update all three places.
#
# `linkedin` is NOT an ATS — it's a profile site — but it lives in this
# list so each resume can carry a LinkedIn profile URL (and optionally
# the email/password the user uses to log in, for future auto-connect
# features). The existing `profile_url` field on the credential row is
# what stores the LinkedIn profile; the `email` + `password` are
# optional-but-present so the shape matches the ATS credentials and
# the frontend can render one consistent form.
SUPPORTED_PLATFORM_LITERALS = Literal[
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters",
    "recruitee", "bamboohr", "jobvite", "wellfound", "himalayas",
    "linkedin",
]

# URL schemes permitted on `profile_url`. Rendered by the frontend as
# `<a href={cred.profile_url} target="_blank">` in CredentialsPage.tsx,
# so anything other than these lets an attacker (or a confused user)
# plant a click-triggered XSS payload.
_URL_SAFE_SCHEMES = ("http://", "https://", "/")


def _validate_optional_url(v: str | None) -> str | None:
    if v is None or v == "":
        return v
    stripped = v.strip()
    if not stripped:
        return ""
    low = stripped.lower()
    if not low.startswith(_URL_SAFE_SCHEMES):
        raise ValueError(
            "profile_url must start with http://, https://, or / (relative)"
        )
    return stripped


class CredentialCreate(BaseModel):
    """Body of POST /api/v1/credentials/{resume_id}.

    Upsert shape: if a credential already exists for
    `(resume_id, platform)`, the endpoint updates it in place; otherwise
    it creates a new row. `password` is optional so the caller can
    update `email` or `profile_url` without re-submitting the password.
    """

    # F306: ``extra="forbid"`` so a typoed field name (``passowrd``)
    # 422s instead of silently dropping. Same defense pattern as
    # F128/F130/F162/F268/F305.
    model_config = ConfigDict(extra="forbid")

    platform: SUPPORTED_PLATFORM_LITERALS
    email: EmailStr
    # Plaintext password — encrypted with Fernet before storage in
    # `encrypted_password` (utils/crypto.encrypt_credential). 500-char
    # cap is far more than any real password; it stops DB-column
    # overflow attacks where a 10 MB password would generate a many-MB
    # Fernet ciphertext.
    password: str | None = Field(default=None, max_length=500)
    # Profile URL is rendered as a clickable <a href> in the frontend.
    # max_length matches the DB column (String(500)). Validator below
    # enforces scheme allowlist.
    profile_url: str | None = Field(default=None, max_length=500)

    @field_validator("profile_url")
    @classmethod
    def _check_profile_url(cls, v):
        return _validate_optional_url(v)
