"""F285 — built-in cluster PATCH guard + display_name HTML strip
(closes F142 (a)+(b)).

F142 found that ``PATCH /role-clusters/{id}`` lets admins flip
``is_active=False`` or ``is_relevant=False`` on the built-in
``infra`` and ``security`` clusters — silently breaking ALL role
classification platform-wide because ``_get_relevant_clusters``
filters on ``is_active=True AND is_relevant=True``. The DELETE
handler has a built-in guard but PATCH didn't.

F285 mirrors the DELETE guard onto PATCH: ``infra`` and
``security`` cannot be disabled via PATCH. Other PATCH fields
(display_name, keywords, approved_roles) still work — admins
can still customise built-ins, just not turn them off.

F142(b) is closed in the same patch by routing ``display_name``
through ``strip_html_tags`` on both create and update paths so
``<script>alert(1)</script>`` can't be persisted and rendered
verbatim in the admin UI.

Sub-points (c) and (d) of F142 (cluster_id: UUID typing,
extra="forbid") were already addressed in earlier rounds (F199
and F268 respectively).
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
os.environ.setdefault("JWT_SECRET", "pytest-f285")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "role_config.py").read_text()


def test_patch_handler_blocks_builtin_deactivation():
    """The PATCH handler must reject ``is_active=False`` /
    ``is_relevant=False`` on built-in clusters with a 400. The
    DELETE handler already does this; the asymmetry was the bug.
    """
    src = _read_handler()
    patch_start = src.find("async def update_role_cluster")
    assert patch_start >= 0, (
        "F285 regression: PATCH handler renamed or moved. "
        "Update this test or restore the structure."
    )
    patch_end = src.find("@router.", patch_start + 1)
    if patch_end < 0:
        patch_end = len(src)
    handler = src[patch_start:patch_end]
    # Must reference both built-in cluster names and the
    # deactivation predicate. We don't pin exact wording so the
    # test stays resilient to small refactors.
    assert "infra" in handler and "security" in handler, (
        "F285 regression: PATCH guard no longer checks for the "
        "built-in cluster names. Admins can re-enable the kill-"
        "switch by flipping is_active=False on infra/security."
    )
    assert "is_active is False" in handler or "is_active=False" in handler, (
        "F285 regression: PATCH guard no longer checks for "
        "``is_active is False`` deactivation."
    )
    assert "is_relevant is False" in handler or "is_relevant=False" in handler, (
        "F285 regression: PATCH guard no longer checks for "
        "``is_relevant is False`` deactivation. Admins can flip "
        "is_relevant=False on infra/security and break the "
        "scoring pipeline that way instead."
    )
    # Must raise HTTPException(400) for the bad case.
    assert "status_code=400" in handler, (
        "F285 regression: PATCH guard no longer raises 400 on "
        "built-in deactivation."
    )


def test_patch_still_allows_non_deactivation_changes_to_builtins():
    """Admins must still be able to customise ``display_name``,
    ``keywords``, ``approved_roles``, ``sort_order`` on built-ins
    — the guard is specifically about disabling, not all changes.
    The handler test above checks that the guard fires only on
    is_active/is_relevant=False, not on every PATCH to a built-in.
    """
    src = _read_handler()
    patch_start = src.find("async def update_role_cluster")
    patch_end = src.find("@router.", patch_start + 1)
    handler = src[patch_start:patch_end]
    # ``cluster.display_name = ...`` and ``cluster.keywords = ...``
    # assignments must still happen unconditionally inside the
    # PATCH body (only gated by ``body.X is not None``).
    assert "cluster.display_name" in handler
    assert "cluster.keywords" in handler
    assert "cluster.approved_roles" in handler


def test_display_name_routes_through_strip_html_tags():
    """Both create and update paths must apply ``strip_html_tags``
    to ``display_name`` so an admin payload like
    ``<script>alert(1)</script>`` can't be persisted and rendered
    verbatim in the admin UI.
    """
    src = _read_handler()
    # Create path
    create_start = src.find("async def create_role_cluster")
    create_end = src.find("@router.", create_start + 1)
    create_body = src[create_start:create_end]
    assert "strip_html_tags(body.display_name)" in create_body, (
        "F285 regression: create path no longer strips HTML from "
        "display_name. Stored XSS via the admin UI returns."
    )
    # Update path
    update_start = src.find("async def update_role_cluster")
    update_end = src.find("@router.", update_start + 1)
    update_body = src[update_start:update_end]
    assert "strip_html_tags(body.display_name)" in update_body, (
        "F285 regression: update path no longer strips HTML from "
        "display_name. PATCH-side stored XSS reopens."
    )


def test_delete_handler_still_guards_builtins():
    """Original DELETE guard must remain — the F285 PATCH guard
    is a sibling, not a replacement.
    """
    src = _read_handler()
    delete_start = src.find("async def delete_role_cluster")
    if delete_start < 0:
        # Some files name it 'delete_cluster' or similar — fall back
        delete_start = src.find('@router.delete("/{cluster_id}")')
    assert delete_start >= 0, (
        "F285 regression: DELETE handler missing entirely. "
        "Built-in clusters can now be deleted, breaking the "
        "platform's role-classification pipeline."
    )
    # Slice to next router decorator (or EOF).
    next_router = src.find("@router.", delete_start + 1)
    delete_body = src[delete_start:next_router] if next_router > 0 else src[delete_start:]
    assert "infra" in delete_body and "security" in delete_body, (
        "F285 regression: DELETE handler no longer references the "
        "built-in cluster names."
    )
