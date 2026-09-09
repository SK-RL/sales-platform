"""Version-agnostic route enumeration for tests.

FastAPI >= 0.141 (starlette 1.6) makes ``include_router`` lazy at BOTH
levels: ``api_router.routes`` holds ``_IncludedRouter`` objects with no
``.path``/``.methods``, and even the mounted ``app.routes`` only lists
the app's own 9 routes (docs, health, openapi) — the ``/api/v1/*`` tree
is resolved at request time. Seven route-registration tests went red in
CI (unpinned ``fastapi>=0.115`` resolved to 0.141.1) with an empty set
or ``AttributeError: '_IncludedRouter' object has no attribute 'path'``,
while passing locally on 0.136 where both are still flattened.

Verified against both versions: ``app.openapi()["paths"]`` is fully
resolved everywhere (225 routes on 0.141.1, matching 0.136 minus HEAD /
docs-only entries) and nothing in ``app/`` uses ``include_in_schema=
False``, so it is complete. Enumerate through that. Leaf routers
(``applications.router`` etc.) are unaffected and may still be inspected
directly.
"""
from __future__ import annotations

from functools import lru_cache

_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}


@lru_cache(maxsize=1)
def registered_routes() -> frozenset[tuple[str, str]]:
    """``{(METHOD, "/api/v1/…"), …}`` for every documented HTTP route."""
    from app.main import app

    paths = app.openapi().get("paths", {})
    return frozenset(
        (method.upper(), path)
        for path, ops in paths.items()
        for method in ops
        if method.lower() in _HTTP_VERBS
    )


def registered_paths() -> frozenset[str]:
    return frozenset(p for _, p in registered_routes())
