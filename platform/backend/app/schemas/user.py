from pydantic import BaseModel, ConfigDict, EmailStr, Field
from datetime import datetime
from uuid import UUID


class UserOut(BaseModel):
    id: UUID
    email: str
    name: str
    avatar_url: str
    role: str
    is_active: bool
    active_resume_id: UUID | None = None
    has_password: bool = False
    # F247: surfaced so the frontend can route to the change-password
    # screen when a super-admin has force-reset the user's password.
    # Defaults to False so old clients that ignore the field aren't
    # affected, but the new client checks it after every login + every
    # ``/auth/me`` call and locks navigation until the user changes it.
    must_change_password: bool = False
    created_at: datetime
    last_login_at: datetime | None

    model_config = {"from_attributes": True}

    @classmethod
    def from_user(cls, user) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            avatar_url=user.avatar_url or "",
            role=user.role,
            is_active=user.is_active,
            active_resume_id=user.active_resume_id,
            has_password=bool(user.password_hash),
            # ``getattr`` with a False default keeps this safe to call
            # against any User-shaped object that predates the column
            # (test factories, etc.). On real ORM rows the column is
            # NOT NULL so the attribute always exists.
            must_change_password=getattr(user, "must_change_password", False),
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )


class UserUpdate(BaseModel):
    # F268 — extra="forbid" on user-mutation schemas. These are super_admin
    # surfaces, so the audience is small + trusted, but typos still hide
    # bugs (e.g. ``is_acitve`` would silent-no-op pre-fix). Same regression
    # class as the F194 PATCH /applications fix.
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    is_active: bool | None = None


# F290 (closes F153 a) — bound every password-carrying field at
# the schema layer.
#
# Pre-fix ``password: str`` had no length cap, so a 1 MB password
# in a login attempt held the server's connection for ~76s
# (mostly network I/O, but the backend still ran SHA-256 + bcrypt
# on every byte). 128 chars is well above any reasonable
# passphrase (the longest practical xkcd-936 four-word passphrase
# is ~50 chars) and well under the bcrypt 72-byte input window
# that the SHA-256 pre-hash collapses anything longer into. We
# cap at 128 specifically rather than 72 to leave headroom for
# future migration to argon2 (which doesn't have the bcrypt
# 72-byte cap) without breaking existing passwords.
#
# Min length comes from the existing /change-password +
# /reset-password/confirm handler (F43: 8 chars per OWASP/NIST
# SP 800-63B). Hoisting that to the schema layer means a future
# refactor that drops the handler check still has the floor.
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 128


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    name: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)
    role: str = "viewer"


class ChangePassword(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # F290: cap current_password at the same ceiling — even though
    # we only ever check it via constant-time bcrypt compare, a 1MB
    # ``current_password`` in the request body still has to be
    # parsed by Pydantic + sent through the SHA-256 pre-hash, which
    # is gratuitous server work for an attacker who knows their
    # current password is wrong anyway.
    current_password: str = Field(..., max_length=PASSWORD_MAX_LEN)
    new_password: str = Field(..., min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class ResetPasswordConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # F290: tokens are 32-byte URL-safe random; a generous 256-char
    # cap is plenty even with future token-format changes.
    token: str = Field(..., min_length=10, max_length=256)
    new_password: str = Field(..., min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)
