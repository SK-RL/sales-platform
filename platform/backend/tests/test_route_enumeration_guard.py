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
_DIRECT = re.compile(r"\bapi_router\.routes\b")


def test_no_test_enumerates_api_router_routes_directly():
    offenders = sorted(
        p.name for p in _TESTS.glob("test_*.py")
        if p.name != pathlib.Path(__file__).name
        and _DIRECT.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == [], (
        f"use tests._routes.registered_routes() instead of api_router.routes in: {offenders}"
    )


def test_registered_routes_is_flat_and_non_empty():
    from tests._routes import registered_routes

    routes = registered_routes()
    assert len(routes) > 100
    # Every entry is a real (METHOD, path) pair — nothing lazy leaked through.
    assert all(isinstance(m, str) and p.startswith("/") for m, p in routes)
    assert ("GET", "/api/v1/jobs") in routes
