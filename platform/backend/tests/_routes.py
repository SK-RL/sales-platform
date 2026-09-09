"""Version-agnostic route enumeration for tests.

FastAPI >= 0.141 (starlette 1.6) makes ``APIRouter.include_router`` lazy:
the aggregator ``api_router.routes`` then holds ``_IncludedRouter``
objects with no ``.path`` / ``.methods`` until the router is mounted on
an app. Every test that did ``for r in api_router.routes`` broke in CI
(unpinned ``fastapi>=0.115`` resolved to 0.141.1) while passing locally
on 0.136 — seven route-registration tests went red with an empty set or
``AttributeError: '_IncludedRouter' object has no attribute 'path'``.

``app.routes`` on the mounted FastAPI app is flattened on every version,
so enumerate through that. Leaf routers (``applications.router`` etc.)
are unaffected and may still be inspected directly.
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def registered_routes() -> frozenset[tuple[str, str]]:
    """``{(METHOD, "/api/v1/…"), …}`` for every HTTP route on the app."""
    from app.main import app

    return frozenset(
        (m, r.path)
        for r in app.routes
        for m in (getattr(r, "methods", None) or set())
    )


def registered_paths() -> frozenset[str]:
    return frozenset(p for _, p in registered_routes())
