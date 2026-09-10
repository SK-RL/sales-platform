"""Drift guard for tests/_routes.py.

Tests must not enumerate the aggregator ``api_router.routes`` directly —
on FastAPI >= 0.141 it holds lazy ``_IncludedRouter`` objects and the
enumeration silently yields an empty set (or an AttributeError). Use
``tests._routes.registered_routes()`` which reads the mounted app.
"""
from __future__ import annotations

import os
import pathlib
import re

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-routes")

_TESTS = pathlib.Path(__file__).parent
# Both aggregators are lazy on FastAPI >= 0.141, so neither may be
# enumerated. The app-level form was added after a test reading the
# mounted app's route list passed locally and went red on main — the
# guard only covered the api_router form.
#
# Matched with the AST rather than a regex: the first attempt used a
# pattern and immediately flagged its own explanatory docstring, because
# a regex can't tell code from prose. Walking the tree can.
_FORBIDDEN_OWNERS = frozenset({"api_router", "app", "fastapi_app"})


def _enumerates_routes(source: str) -> bool:
    """True when the module reads ``<aggregator>.routes`` in real code."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "routes"
            and isinstance(node.value, ast.Name)
            and node.value.id in _FORBIDDEN_OWNERS
        ):
            return True
    return False


def test_no_test_enumerates_api_router_routes_directly():
    offenders = sorted(
        p.name for p in _TESTS.glob("test_*.py")
        if p.name != pathlib.Path(__file__).name
        and _enumerates_routes(p.read_text(encoding="utf-8"))
    )
    assert offenders == [], (
        "use tests._routes.registered_routes() instead of enumerating a "
        f"router/app .routes list in: {offenders}"
    )


def test_registered_routes_is_flat_and_non_empty():
    from tests._routes import registered_routes

    routes = registered_routes()
    assert len(routes) > 100, (
        f"only {len(routes)} routes — on FastAPI >= 0.141 a lazy include "
        "leaked through; the helper must read app.openapi()['paths']"
    )
    # Every entry is a real (METHOD, path) pair — nothing lazy leaked through.
    assert all(isinstance(m, str) and p.startswith("/") for m, p in routes)
    assert ("GET", "/api/v1/jobs") in routes


def test_helper_does_not_depend_on_app_routes():
    """``app.routes`` is only the app's own 9 routes on FastAPI >= 0.141;
    the helper must go through the fully-resolved OpenAPI schema."""
    import inspect
    from tests import _routes

    src = inspect.getsource(_routes.registered_routes)
    assert "openapi()" in src
    assert "app.routes" not in src
