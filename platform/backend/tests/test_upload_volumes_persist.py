"""Regression lock: user-uploaded files must live on persistent volumes.

Incident (feedback 650514ad, "Unable to Download Document", critical):
the backend service in docker-compose.prod.yml mounted only the
``backups`` volume. Profile KYC documents
(``/var/lib/sales-platform/profile-docs``) and feedback attachments
(``/app/uploads``) were written to the container's *writable layer*, so
every deploy — which recreates the backend container from the freshly
pulled GHCR image — silently DESTROYED them. The DB rows (on the
postgres volume) survived and kept pointing at the now-missing bytes,
so ``GET …/documents/{id}/download`` returned 410 Gone. 27 documents
were lost with zero recovery path.

The forward fix is a docker-compose concern, so — like
``test_f273_uvicorn_multi_worker`` and ``test_celery_oom_hardening`` —
this guards the *compose file* itself (text-level, no yaml dep, no live
Docker). If someone drops these mounts, uploads become deploy-ephemeral
again and this fails loudly.

Resumes are intentionally NOT covered here: they persist their bytes in
a Postgres column (``resumes.file_data``), not on disk.
"""

from __future__ import annotations

import pathlib

_PLATFORM_DIR = pathlib.Path(__file__).resolve().parents[2]
_PROD_COMPOSE = _PLATFORM_DIR / "docker-compose.prod.yml"

# The default storage root the app uses when PROFILE_DOC_ROOT is unset
# (app/utils/profile_doc_storage.py). Mount target must match it or the
# volume protects the wrong path.
_PROFILE_DOC_ROOT = "/var/lib/sales-platform/profile-docs"
_FEEDBACK_UPLOAD_PARENT = "/app/uploads"


def test_backend_mounts_profile_docs_volume():
    src = _PROD_COMPOSE.read_text()
    assert f"profile-docs:{_PROFILE_DOC_ROOT}" in src, (
        "docker-compose.prod.yml backend service no longer mounts a "
        "persistent volume at the profile-doc storage root. Uploaded KYC "
        "documents will be wiped on the next deploy (incident 650514ad)."
    )


def test_backend_mounts_feedback_uploads_volume():
    src = _PROD_COMPOSE.read_text()
    assert f"uploads:{_FEEDBACK_UPLOAD_PARENT}" in src, (
        "docker-compose.prod.yml backend service no longer mounts a "
        "persistent volume at /app/uploads — feedback attachments will "
        "be wiped on the next deploy."
    )


def test_named_volumes_declared():
    src = _PROD_COMPOSE.read_text()
    # Declared in the top-level ``volumes:`` map (compose errors if a
    # service references an undeclared named volume).
    for name in ("profile-docs:", "uploads:"):
        assert f"\n  {name}" in src, (
            f"Named volume {name!r} is referenced by the backend service "
            f"but not declared in the top-level volumes map."
        )


def test_storage_root_default_matches_mount():
    """Drift guard: if the app's default storage root changes, the mount
    target above must change with it, or uploads land outside the
    volume and the persistence fix silently stops working."""
    storage = (
        _PLATFORM_DIR / "backend" / "app" / "utils" / "profile_doc_storage.py"
    ).read_text()
    assert f'"{_PROFILE_DOC_ROOT}"' in storage, (
        "profile_doc_storage._DEFAULT_ROOT changed but the compose mount "
        f"still targets {_PROFILE_DOC_ROOT!r}. Update both together."
    )
