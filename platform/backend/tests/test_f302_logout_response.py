"""F302 — /auth/logout returns JSON + matching cookie attributes (closes F210).

F210 found POST /auth/logout returned HTTP 307 RedirectResponse to
$APP_URL with ``response.delete_cookie("session")`` — two issues:

  (a) An API endpoint shouldn't serve UI navigation. The SPA's
      ``fetch("/auth/logout", {method:"POST"})`` followed the 307,
      downloaded the SPA HTML (~500KB), ``res.json()`` failed,
      wasted bandwidth.
  (b) ``delete_cookie()`` with no args drops the ``Secure;
      HttpOnly`` attributes login set, so the clearing header
      doesn't match the cookie it's replacing — security-scanner
      lint flag (Burp/ZAP) + theoretical weakness under mixed-
      content / downgrade scenarios.

F302 ships:
  * JSONResponse instead of RedirectResponse — frontend handles
    its own navigation
  * delete_cookie passes ``path``, ``secure``, ``httponly``,
    ``samesite`` so the clearing header matches login's cookie
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
os.environ.setdefault("JWT_SECRET", "pytest-f302")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_logout_returns_json_not_redirect():
    """The handler must return JSONResponse, not RedirectResponse."""
    src = (_BACKEND / "app" / "api" / "v1" / "auth.py").read_text()
    handler_start = src.find("async def logout")
    handler_end = src.find("@router.", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    handler = src[handler_start:handler_end]
    # Pre-fix used `RedirectResponse(url=...)`. Post-fix uses
    # JSONResponse with an ``ok: True`` payload.
    assert "JSONResponse(" in handler, (
        "F302 regression: logout no longer returns JSONResponse. "
        "The handler is back to a redirect — frontend round-trips "
        "the full SPA HTML on every logout."
    )
    assert "RedirectResponse(" not in handler, (
        "F302 regression: logout reverted to RedirectResponse. "
        "API endpoints shouldn't serve UI navigation."
    )


def test_logout_delete_cookie_includes_secure_httponly():
    """The cookie-clearing header must carry the same Secure +
    HttpOnly + SameSite attributes that login set, otherwise
    security scanners flag the mismatch and the clearing is
    technically weaker.
    """
    src = (_BACKEND / "app" / "api" / "v1" / "auth.py").read_text()
    handler_start = src.find("async def logout")
    handler_end = src.find("@router.", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    handler = src[handler_start:handler_end]
    # delete_cookie call must pass the security attrs.
    assert "secure=True" in handler, (
        "F302 regression: logout's delete_cookie no longer passes "
        "``secure=True``. Clearing header attrs don't match login's "
        "set-cookie — Burp/ZAP lint flag."
    )
    assert "httponly=True" in handler, (
        "F302 regression: logout's delete_cookie no longer passes "
        "``httponly=True``."
    )
    assert "samesite=" in handler, (
        "F302 regression: logout's delete_cookie no longer passes "
        "``samesite``."
    )
    assert 'path="/"' in handler, (
        "F302 regression: logout's delete_cookie no longer passes "
        "``path=\"/\"``. Without explicit path, the clearing may "
        "not match cookies set under specific paths."
    )
