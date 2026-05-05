"""F328 — race-safe ``POST /role-clusters`` admin endpoint.

Pre-fix the create_role_cluster handler did a lookup-then-INSERT
pattern: SELECT for an existing cluster by name, raise 409 if
one exists, otherwise db.add(RoleClusterConfig(...)) + commit.
Two concurrent admin POSTs with the same normalized name both
passed the lookup, both INSERTed, and the second blew up with
an unhandled IntegrityError on the
``role_cluster_configs.name`` UNIQUE constraint that escaped to
the client as a bare HTTP 500.

F328 wraps the commit in try/except IntegrityError, matches on
the ``role_cluster_configs_name`` substring (Postgres
autogenerates ``role_cluster_configs_name_key`` for unnamed
UNIQUE-on-column constraints), and re-raises as the same 409
the lookup-check produces.

Lowest exposure of the F325-F328 race-safe sweep (admin-gated +
clusters added rarely) but completes the consistent shape across
all CREATE handlers.
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
os.environ.setdefault("JWT_SECRET", "pytest-f328")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "role_config.py").read_text()


def _read_model() -> str:
    return (_BACKEND / "app" / "models" / "role_config.py").read_text()


def test_handler_imports_integrity_error():
    src = _read_handler()
    assert "from sqlalchemy.exc import IntegrityError" in src


def test_create_handler_wraps_commit_in_try_except():
    src = _read_handler()
    add_idx = src.find("db.add(cluster)")
    assert add_idx > 0
    window = src[add_idx:add_idx + 3000]
    assert "try:" in window, (
        "F328 regression: db.add(cluster) is no longer followed by "
        "a try block. Concurrent same-name POSTs will 500 again."
    )
    assert "except IntegrityError" in window, (
        "F328 regression: race-recovery branch removed."
    )


def test_create_handler_constraint_name_match_present():
    src = _read_handler()
    add_idx = src.find("db.add(cluster)")
    window = src[add_idx:add_idx + 3000]
    assert "role_cluster_configs_name" in window, (
        "F328 regression: constraint-name match removed. Handler "
        "would translate ALL IntegrityErrors to 409 now."
    )


def test_create_handler_409_message_byte_identical_to_lookup_check():
    src = _read_handler()
    # Lookup-check 409 uses
    # ``f"Role cluster '{name}' already exists"``.
    assert "Role cluster '{name}' already exists" in src
    add_idx = src.find("db.add(cluster)")
    window = src[add_idx:add_idx + 3000]
    assert "Role cluster '{name}' already exists" in window, (
        "F328 regression: race branch 409 message diverges from the "
        "lookup-check 409 message."
    )


def test_create_handler_does_not_blanket_translate_integrity_errors():
    src = _read_handler()
    add_idx = src.find("db.add(cluster)")
    window = src[add_idx:add_idx + 3000]
    assert "        raise\n" in window, (
        "F328 regression: race branch swallows ALL IntegrityErrors "
        "as 409. Only the name-collision case should translate; "
        "other constraint failures must propagate."
    )


def test_role_cluster_model_name_unique_intact():
    src = _read_model()
    assert "name:" in src
    assert "unique=True" in src, (
        "F328 regression: RoleClusterConfig.name UNIQUE constraint "
        "dropped — the race fix is now moot but duplicate names "
        "will silently succeed at the DB layer."
    )
    assert '__tablename__ = "role_cluster_configs"' in src, (
        "F328 regression: role_cluster_configs table renamed — "
        "the constraint-name match in the handler now misses."
    )
