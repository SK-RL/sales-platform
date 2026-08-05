"""Security regression: feedback attachments must be un-cacheable.

Auth-bypass-via-CDN-cache (found during in-depth download testing):
``GET /api/v1/feedback/attachments/{filename}`` is per-user authorized,
but the filename ends in the stored extension (…/<uuid>.png). Cloudflare
caches static extensions (.png/.jpg/.pdf/…) by default, so without an
explicit no-cache directive the first *authorized* fetch populated the
edge cache and every later *unauthenticated* request for that URL was
served the bytes straight from Cloudflare — a full auth bypass on other
users' uploaded attachments. Verified live: unauth-first returned 401,
then 200 after an authed fetch primed the cache.

Fix: the handler now returns ``Cache-Control: private, no-store`` so
Cloudflare never caches it. This test is a source-level guard (matches
the suite's no-live-DB style) so the header can't be dropped again.
"""

from __future__ import annotations

import inspect
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
os.environ.setdefault("JWT_SECRET", "pytest-attach-cache")


def _get_attachment_src() -> str:
    from app.api.v1.feedback import get_attachment

    return inspect.getsource(get_attachment)


def test_attachment_download_sets_no_store():
    src = _get_attachment_src()
    assert "Cache-Control" in src, (
        "feedback attachment download no longer sets Cache-Control — "
        "Cloudflare will cache it by extension and serve it to "
        "unauthenticated requests (auth bypass)."
    )
    # ``no-store`` (and/or ``private``) is what actually tells Cloudflare
    # not to cache. A bare ``max-age`` would make it worse, not better.
    assert "no-store" in src, (
        "feedback attachment Cache-Control must include 'no-store' so the "
        "CDN never caches per-user KYC-adjacent files."
    )


def test_attachment_download_still_requires_auth():
    """The cache header is defence-in-depth; the dependency-level auth
    must also remain (regression #21 originally added it)."""
    src = _get_attachment_src()
    assert "get_current_user" in src
    # Owner-or-admin check must survive too.
    assert "user.role not in" in src and "owner_id" in src
