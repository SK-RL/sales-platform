"""Authentication endpoints: email/password login, register, password management + Google OAuth2."""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from authlib.integrations.starlette_client import OAuth
from jose import jwt
import bcrypt

from app.config import get_settings
from app.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_role
from app.schemas.user import UserOut, UserCreate, ChangePassword, ResetPasswordRequest, ResetPasswordConfirm
from app.utils.rate_limit import login_limiter

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

oauth = OAuth()
if settings.google_client_id:
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

VALID_ROLES = {"admin", "reviewer", "viewer"}


_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")

# F163: pre-computed bcrypt hash of a random blob used to burn CPU on
# login attempts against non-existent accounts, so response-time
# measurements can't be used to enumerate valid emails. The plaintext
# that produced this hash is not stored anywhere — a successful compare
# is impossible, which is the point. Cost factor MUST match
# `_hash_password` (rounds=12) so the timing distribution overlaps.
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(
    hashlib.sha256(secrets.token_bytes(32)).digest(),
    bcrypt.gensalt(rounds=12),
)


def _hash_password(password: str) -> str:
    """Hash a new password with bcrypt (cost=12).

    Regression finding 23: this used to be a single-round SHA-256 with a
    global (jwt_secret-derived) salt — no key stretching, no per-user
    salt, trivially crackable from a DB dump on consumer GPUs. We now
    use bcrypt (per-hash random salt, adaptive work factor). Existing
    SHA-256 hashes are still accepted by `_verify_password` and are
    transparently re-hashed to bcrypt on the user's next successful
    login (see the /login handler).
    """
    # bcrypt has a 72-byte input cap. Pre-hash with SHA-256 so long
    # passwords aren't silently truncated — this is the standard
    # workaround and is safe (SHA-256 output is uniform binary).
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode("utf-8")


def _legacy_sha256(password: str) -> str:
    """Reproduce the old SHA-256 hash so we can still verify pre-migration users."""
    salted = f"{settings.jwt_secret}:{password}"
    return hashlib.sha256(salted.encode()).hexdigest()


def _verify_password(password: str, password_hash: str) -> bool:
    """Verify against either a bcrypt hash or a legacy SHA-256 hash."""
    if not password_hash:
        return False
    if password_hash.startswith(_BCRYPT_PREFIXES):
        try:
            digest = hashlib.sha256(password.encode("utf-8")).digest()
            return bcrypt.checkpw(digest, password_hash.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    # Legacy path — constant-time compare to defeat timing oracles that
    # the old `==` comparison opened up.
    return hmac.compare_digest(_legacy_sha256(password), password_hash)


def _is_legacy_hash(password_hash: str | None) -> bool:
    return bool(password_hash) and not password_hash.startswith(_BCRYPT_PREFIXES)


def _hash_reset_token(token: str) -> str:
    """Deterministic keyed hash for password-reset tokens.

    The plaintext token is a 32-byte URL-safe random value, so even a
    plain SHA-256 would be secure against brute force; using HMAC with
    `jwt_secret` adds defense-in-depth so a DB dump alone cannot forge a
    valid reset without also leaking the secret. This MUST stay
    deterministic (unlike `_hash_password`, which is now bcrypt with a
    random salt) so that the reset-confirm endpoint can look the hash
    up by equality.
    """
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _mint_jwt(user: User) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    return jwt.encode(
        {"sub": str(user.id), "email": user.email, "role": user.role, "exp": exp},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


# F290 (closes F153 a) — cap LoginRequest.password at the same
# ceiling ``ChangePassword`` / ``ResetPasswordConfirm`` use. A
# 1 MB password in a login attempt held the server connection for
# ~76s pre-fix (mostly network I/O, but the backend still ran
# SHA-256 + bcrypt on every byte). Single source of truth for all
# password-carrying fields lives in ``schemas/user.py``.
# NB: no min_length here — wrong-credential 401 must look the
# same whether the password is too short or just wrong, so
# rejecting "too short" with a 422 would itself be a length-
# oracle. The handler's ``_verify_password`` constant-time compare
# handles short passwords as "wrong" and increments the rate-limit
# counter, which is the correct behaviour.
from app.schemas.user import PASSWORD_MAX_LEN as _LOGIN_PASSWORD_MAX


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=_LOGIN_PASSWORD_MAX)


def _rate_limit_key(request: Request, email: str) -> str:
    """Key the rate limiter on (client IP, email).

    Keying on both means a single attacker can't burn a victim's counter
    to lock them out, and a victim behind a shared IP (office / VPN)
    isn't locked out by an unrelated attacker hitting a different email.
    X-Forwarded-For is honoured when present because the reverse proxy
    strips the real client IP from the raw socket; if absent we fall
    back to the socket peer.
    """
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = fwd or (request.client.host if request.client else "unknown")
    return f"{ip}|{email.lower()}"


@router.post("/login")
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Email/password login. Returns JWT in a cookie and as JSON."""
    rl_key = _rate_limit_key(request, body.email)
    limited, retry_after = await login_limiter.is_limited(rl_key)
    if limited:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Please wait and try again.",
            headers={"Retry-After": str(retry_after)},
        )

    result = await db.execute(select(User).where(User.email == body.email, User.is_active == True))
    user = result.scalar_one_or_none()

    # F163: don't short-circuit on "user not found" — bcrypt.checkpw is
    # the dominant cost of a login attempt (~200ms at rounds=12), and
    # branching early turns that cost into an enumeration oracle. Burn
    # an equivalent bcrypt compare against a dummy hash so the
    # non-existent and wrong-password paths take the same wall-clock
    # time.
    if not user or not user.password_hash:
        try:
            bcrypt.checkpw(
                hashlib.sha256(body.password.encode("utf-8")).digest(),
                _DUMMY_BCRYPT_HASH,
            )
        except (ValueError, TypeError):
            pass
        await login_limiter.record_failure(rl_key)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not _verify_password(body.password, user.password_hash):
        await login_limiter.record_failure(rl_key)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Successful auth — clear this key's counter so a user who mistyped
    # a few times isn't still cooling down after they log in correctly.
    await login_limiter.record_success(rl_key)

    # Lazy migration to bcrypt: if this user's stored hash is still the
    # legacy SHA-256 format, we just verified the plaintext — take this
    # one chance to upgrade them without forcing a reset.
    if _is_legacy_hash(user.password_hash):
        user.password_hash = _hash_password(body.password)

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)

    token = _mint_jwt(user)
    response = JSONResponse(content={
        "token": token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "avatar_url": user.avatar_url,
            # F247: include the force-change flag so the frontend can
            # route straight to the change-password screen after a
            # super-admin reset, without a separate /auth/me round-trip.
            "must_change_password": bool(getattr(user, "must_change_password", False)),
        },
    })
    response.set_cookie(
        key="session", value=token,
        httponly=True, samesite="lax", secure=True,
        max_age=settings.jwt_expire_hours * 3600,
    )
    return response


@router.post("/register")
async def register(
    body: UserCreate,
    user: User = Depends(require_role("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Register a new user (super_admin only)."""
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(VALID_ROLES)}")

    # F297 (closes F153 b): bump from 6 → 8 to match the schema-
    # layer floor (PASSWORD_MIN_LEN in schemas/user.py, F290) AND
    # the /change-password + /reset-password/confirm handlers
    # below. Pre-fix /auth/register accepted 6-char passwords
    # while every other handler required 8 — locked-out admins
    # who got reset to a 6-char password couldn't change it
    # without hitting the higher floor. The schema's
    # ``min_length=PASSWORD_MIN_LEN`` already 422s shorter
    # passwords at parse time so this branch is unreachable
    # post-F290; kept as defense-in-depth in case the schema is
    # ever relaxed.
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    import uuid
    new_user = User(
        id=uuid.uuid4(),
        email=body.email,
        name=body.name,
        password_hash=_hash_password(body.password),
        role=body.role,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    return {
        "id": str(new_user.id),
        "email": new_user.email,
        "name": new_user.name,
        "role": new_user.role,
        "is_active": new_user.is_active,
        "created_at": new_user.created_at.isoformat(),
    }


@router.post("/change-password")
async def change_password(
    body: ChangePassword,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change current user's password."""
    if not user.password_hash:
        raise HTTPException(status_code=400, detail="Account uses Google OAuth, no password to change")

    if not _verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    # Regression finding 43: raise the server-side minimum from 6 to 8 to
    # match OWASP and NIST SP 800-63B guidance. The Settings password form
    # used to advertise `minlength="6"`, which was below both standards;
    # even if a future frontend bug drops that hint, the API now refuses
    # anything shorter than 8.
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    user.password_hash = _hash_password(body.new_password)
    # F247 regression fix: clear the force-change flag so subsequent
    # logins don't re-trigger the change-password redirect. Set unconditionally
    # so a user who lands here for a normal voluntary change (flag was
    # already false) is a no-op write — keeps the flow uniform.
    user.must_change_password = False
    await db.commit()
    return {"ok": True, "message": "Password changed successfully"}


@router.post("/reset-password/request")
async def request_password_reset(
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Request a password reset. Always returns the same response regardless of email existence.

    Regression finding 163 (RED): the previous implementation branched
    on `if not user` and returned two *different* JSON shapes — the
    existing-email branch included the raw reset token AND a different
    `message` string. That combination was a complete ATO chain:

      1. An unauthenticated attacker could enumerate which emails had
         accounts (CWE-204) by comparing responses across guesses.
      2. Once they confirmed an email, the same endpoint handed them
         the plaintext reset token in the response body with no email
         verification step — a straight path to owning the account.

    The fix: (a) never echo the token in the API response (it now only
    reaches the user via out-of-band email delivery — TODO wire up the
    mail transport in prod), and (b) return a byte-for-byte identical
    response on both paths so a caller cannot distinguish valid from
    invalid emails.
    """
    result = await db.execute(select(User).where(User.email == body.email, User.is_active == True))
    user = result.scalar_one_or_none()

    if user:
        token = secrets.token_urlsafe(32)
        user.password_reset_token = _hash_reset_token(token)
        user.password_reset_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.commit()
        # F163: token is intentionally NOT in the response body. In
        # production this is delivered via email — the send hook lives
        # outside this endpoint so a mail-transport outage doesn't
        # change the observable timing of the reset request (which
        # would be a new enumeration oracle).

    # F163: single response shape regardless of whether the email
    # exists. Do not add fields, do not vary `message`, do not change
    # status codes. Everything that could be used to distinguish the
    # two branches must live behind the email channel.
    return {"ok": True, "message": "If the email exists, a reset link has been sent"}


@router.post("/reset-password/confirm")
async def confirm_password_reset(
    body: ResetPasswordConfirm,
    db: AsyncSession = Depends(get_db),
):
    """Confirm password reset using token."""
    # Regression finding 43: OWASP/NIST-aligned minimum of 8 chars. Matches
    # the check on /change-password so either code path enforces the same
    # floor. Longer existing passwords never hit this branch.
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    token_hash = _hash_reset_token(body.token)
    result = await db.execute(
        select(User).where(
            User.password_reset_token == token_hash,
            User.is_active == True,
        )
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    if user.password_reset_expires and user.password_reset_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Reset token has expired")

    user.password_hash = _hash_password(body.new_password)
    user.password_reset_token = None
    user.password_reset_expires = None
    await db.commit()

    return {"ok": True, "message": "Password reset successfully"}


@router.get("/google")
async def google_login(request: Request):
    if not settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")
    redirect_uri = f"{settings.api_url}/api/v1/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        raise HTTPException(status_code=400, detail="Failed to get user info from Google")

    email = userinfo["email"]
    google_sub = userinfo["sub"]

    # Check allow-list (if configured)
    allowed = settings.allowed_email_list
    if allowed and email not in allowed:
        raise HTTPException(status_code=403, detail="Email not authorized")

    # Find existing user by google_sub or email
    result = await db.execute(select(User).where(User.google_sub == google_sub))
    user = result.scalar_one_or_none()

    if not user:
        # Check if admin pre-created this user by email (invite-only)
        result = await db.execute(select(User).where(User.email == email, User.is_active == True))
        user = result.scalar_one_or_none()
        if user:
            # Link Google account to existing invited user
            user.google_sub = google_sub

    if not user:
        # No pre-created account — reject (invite-only mode)
        return RedirectResponse(url=settings.app_url + "/login?error=not_invited")

    user.last_login_at = datetime.now(timezone.utc)
    user.name = userinfo.get("name", user.name)
    user.avatar_url = userinfo.get("picture", user.avatar_url)

    await db.commit()
    await db.refresh(user)

    # Set JWT cookie and redirect to frontend
    jwt_token = _mint_jwt(user)
    response = RedirectResponse(url=settings.app_url + "/")
    response.set_cookie(
        key="session", value=jwt_token,
        httponly=True, samesite="lax", secure=True,
        max_age=settings.jwt_expire_hours * 3600,
    )
    return response


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)):
    return UserOut.from_user(user)


@router.post("/logout")
async def logout():
    response = RedirectResponse(url=settings.app_url)
    response.delete_cookie("session")
    return response
