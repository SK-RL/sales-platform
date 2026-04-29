"""F290 — password-field length caps (closes F153 a).

F153(a) found ``LoginRequest.password: str`` had no max_length —
a 1 MB password in a login attempt held the server connection for
~76s while the backend SHA-256 pre-hashed every byte before bcrypt
even ran. Same shape on ``ChangePassword.current_password`` /
``UserCreate.password`` / ``ResetPasswordConfirm.new_password``.

F290 hoists a shared ``PASSWORD_MAX_LEN = 128`` constant + min/max
caps to the schema layer so every password-carrying field 422s at
parse time before any hashing happens. ``LoginRequest`` doesn't
get a ``min_length`` because that would be a length-oracle (a
short-password 422 looks different from a wrong-password 401);
the handler's constant-time compare already handles short inputs
as "wrong".
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
os.environ.setdefault("JWT_SECRET", "pytest-f290")


def test_password_max_len_constant_exists():
    """The shared ``PASSWORD_MAX_LEN`` constant in
    ``schemas/user.py`` is the single source of truth for every
    password-carrying field. A future contributor who introduces
    a new password field should reuse this.
    """
    from app.schemas import user as user_schemas

    assert hasattr(user_schemas, "PASSWORD_MAX_LEN"), (
        "F290 regression: ``PASSWORD_MAX_LEN`` constant removed "
        "from schemas/user.py. New password fields will drift."
    )
    assert hasattr(user_schemas, "PASSWORD_MIN_LEN")
    # 128 is the documented value; pin so a casual lower-bound
    # change requires touching this test.
    assert user_schemas.PASSWORD_MAX_LEN == 128, (
        "F290 regression: PASSWORD_MAX_LEN value drifted from 128. "
        "If this is intentional, update this test."
    )


def test_login_request_caps_password():
    from app.api.v1.auth import LoginRequest
    import pydantic

    # Just under the cap — accepted.
    LoginRequest(email="x@y.com", password="a" * 128)
    # Over the cap — 422.
    try:
        LoginRequest(email="x@y.com", password="a" * 129)
    except pydantic.ValidationError:
        return
    raise AssertionError(
        "F290 regression: LoginRequest no longer caps the password "
        "field. 1MB password attempts can hold the connection again."
    )


def test_login_request_has_no_min_length():
    """Login MUST NOT enforce a min_length — otherwise the 422
    boundary becomes a length oracle (short-password attempts get a
    different response from too-long ones, which leaks "your guess
    was at least N chars wrong"). The handler's constant-time
    compare handles short inputs by simply not matching.
    """
    from app.api.v1.auth import LoginRequest
    import pydantic

    # 1-char "password" must NOT raise — handler will reject as
    # wrong-credentials 401 (consistent shape).
    try:
        LoginRequest(email="x@y.com", password="a")
    except pydantic.ValidationError as e:
        # ``Pydantic`` 422 would leak the floor; this is wrong.
        raise AssertionError(
            "F290 regression: LoginRequest enforces a min_length "
            "on password. That turns the 422 boundary into a "
            "length oracle for credential probes."
        ) from e


def test_change_password_enforces_min_and_max():
    from app.schemas.user import ChangePassword
    import pydantic

    # Both fields capped.
    ChangePassword(current_password="any", new_password="a" * 8)
    # too-short new_password rejected.
    try:
        ChangePassword(current_password="any", new_password="short")
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError(
            "F290 regression: ChangePassword.new_password no longer "
            "enforces min_length (OWASP/NIST 8-char floor lifted)."
        )
    # over-cap new_password rejected.
    try:
        ChangePassword(current_password="any", new_password="a" * 129)
    except pydantic.ValidationError:
        return
    raise AssertionError(
        "F290 regression: ChangePassword.new_password no longer "
        "enforces max_length."
    )


def test_reset_password_confirm_enforces_caps():
    from app.schemas.user import ResetPasswordConfirm
    import pydantic

    ResetPasswordConfirm(token="a" * 32, new_password="goodpass")
    # token too short
    try:
        ResetPasswordConfirm(token="x", new_password="goodpass")
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError(
            "F290 regression: ResetPasswordConfirm.token no longer "
            "enforces a min_length."
        )
    # password too long
    try:
        ResetPasswordConfirm(token="a" * 32, new_password="a" * 129)
    except pydantic.ValidationError:
        return
    raise AssertionError(
        "F290 regression: ResetPasswordConfirm.new_password no "
        "longer enforces max_length."
    )


def test_user_create_caps_password_and_name():
    from app.schemas.user import UserCreate
    import pydantic

    UserCreate(email="x@y.com", name="Real Name", password="a" * 8)
    # name unbounded was a parallel DoS surface; F290 caps it too.
    try:
        UserCreate(email="x@y.com", name="x" * 201, password="goodpass")
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError(
            "F290 regression: UserCreate.name length cap removed."
        )
    # password over-cap
    try:
        UserCreate(email="x@y.com", name="ok", password="a" * 129)
    except pydantic.ValidationError:
        return
    raise AssertionError(
        "F290 regression: UserCreate.password no longer enforces "
        "max_length."
    )
