"""F340 — aggregator company resolution prefers the authoritative
``companySlug`` over the derived-from-name slug.

User feedback 2026-04-29 (in-app, #5, status=open):

  "On the sales platform, it is showing about 'Senior Cloud
  Engineer' job at 'Ähdus Technology' company and it's URL is
  (https://salesplatform.reventlabs.com/jobs/68fc2102-d04b-406f-
  962a-0a5e94546fe0) but it is showing the job details of a
  different company (DV Trading)..."

Reproduction on prod:
  GET /api/v1/jobs/68fc2102-d04b-406f-962a-0a5e94546fe0
  →  ``company_name`` = "Ähdus Technology" (DB row)
     ``url``         = "https://himalayas.app/companies/elevus/
                        jobs/senior-cloud-engineer"
  GET /api/v1/companies?search=elevus
  →  Elevus (id=0be2fc4d-...) exists as a SEPARATE Company row.

So the job got bound to the wrong Company. Two compounding bugs:

  (1) ``himalayas.py::_normalize`` omitted ``company_name`` from
      its return dict — only ``company_slug``. The aggregator
      path in scan_task fell back to
      ``raw_json.get("companyName", "")`` which on this row
      carried "Ähdus Technology" (stale / wrong value on
      Himalayas's side).

  (2) ``scan_task.py`` derived the lookup slug from the (wrong)
      ``agg_company_name`` via ``re.sub(r"[^a-z0-9-]", "",
      agg_company_name.lower().replace(" ", "-"))`` — so
      "Ähdus Technology" → "hdus-technology" (the Ä is stripped
      as non-ASCII). The job got bound to the existing
      "Ähdus Technology" Company row instead of the
      URL-correct "Elevus" row.

F340 fixes the second bug by preferring the authoritative
``companySlug`` from the upstream raw payload (which builds the
public URL, so it's verifiable) over the name-derived slug.
Resolution order:
  1. raw_json.companySlug (Himalayas key)
  2. raw_json.company_slug (other aggregator's key)
  3. raw_job.company_slug (fetcher-normalised key)
  4. Fallback: derive from agg_company_name (same as pre-fix)

The fetcher-side companion fix (F340 in himalayas.py) surfaces
``company_name`` explicitly in the return dict so downstream
display code doesn't have to root around in raw_json.
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
os.environ.setdefault("JWT_SECRET", "pytest-f340")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_BACKEND / rel).read_text()


def test_himalayas_fetcher_surfaces_company_name():
    """The Himalayas fetcher's _normalize return dict must
    include ``company_name`` so downstream code doesn't have to
    root through ``raw_json`` to display it.
    """
    src = _read("app/fetchers/himalayas.py")
    # Look for the return dict construction body — the
    # company_name key must be one of the emitted keys. We use
    # a generous fixed window because the dict literal contains
    # nested f-strings whose ``{...}`` braces would fool a naive
    # closing-brace search.
    return_idx = src.find("return {\n")
    assert return_idx > 0
    return_block = src[return_idx:return_idx + 2500]
    assert '"company_name"' in return_block, (
        "F340 regression: Himalayas fetcher no longer emits "
        "``company_name`` in its return dict. The aggregator "
        "resolution path falls back to raw_json.companyName, "
        "which can be stale / wrong while companySlug is correct."
    )


def test_scan_task_prefers_authoritative_companyslug():
    """The aggregator-resolution branch in scan_task.py must
    prefer ``raw_json.companySlug`` over deriving the slug from
    the (possibly-wrong) company name.
    """
    src = _read("app/workers/tasks/scan_task.py")
    # Anchor on the aggregator branch.
    agg_idx = src.find("agg_company_name")
    assert agg_idx > 0, "aggregator branch structure changed"
    window = src[agg_idx:agg_idx + 4000]
    # The new fix references companySlug / company_slug from
    # raw_json or raw_job, ahead of the derive-from-name fallback.
    assert "raw_json.get(\"companySlug\")" in window, (
        "F340 regression: aggregator resolution no longer reads "
        "the authoritative ``companySlug`` from raw_json. Jobs "
        "will get re-bound to wrong Company rows when the upstream "
        "name is stale."
    )


def test_scan_task_falls_back_to_derived_slug_when_authoritative_absent():
    """Defense-in-depth: aggregators that don't surface a
    ``companySlug`` field (legacy / future additions) must still
    work via the derived-from-name path. Otherwise the F340 fix
    could BREAK any aggregator that doesn't have an upstream
    slug field.
    """
    src = _read("app/workers/tasks/scan_task.py")
    agg_idx = src.find("agg_company_name")
    window = src[agg_idx:agg_idx + 4000]
    # The derived-from-name path must still exist (the ``else``
    # branch after the authoritative_slug check).
    assert "agg_company_name.lower().replace" in window, (
        "F340 regression: derived-slug fallback removed. "
        "Aggregators without companySlug field will now fail "
        "to bind jobs to companies."
    )


def test_scan_task_derived_slug_uses_same_charset_rules():
    """The new authoritative-slug branch must apply the SAME
    ``[^a-z0-9-]`` character filter as the derived-slug path.
    Otherwise a non-ASCII char in companySlug (rare but possible)
    would skip the cleanup and hit the slug column's NOT NULL /
    length constraints differently than the legacy path.
    """
    src = _read("app/workers/tasks/scan_task.py")
    agg_idx = src.find("authoritative_slug")
    assert agg_idx > 0, "authoritative_slug branch not present"
    window = src[agg_idx:agg_idx + 800]
    assert "[^a-z0-9-]" in window, (
        "F340 regression: authoritative-slug branch no longer "
        "applies the canonical character filter. Risk of "
        "non-ASCII chars hitting the slug column with different "
        "semantics than the derived path."
    )


def test_scan_task_lookup_uses_resolved_slug():
    """The Company lookup must use the (now-correctly-resolved)
    ``agg_slug`` variable. Renaming it would silently break the
    resolution.
    """
    src = _read("app/workers/tasks/scan_task.py")
    agg_idx = src.find("agg_company_name")
    window = src[agg_idx:agg_idx + 5000]
    # The select-by-slug call references agg_slug.
    assert "Company.slug == agg_slug" in window, (
        "F340 regression: Company-by-slug lookup no longer uses "
        "the resolved ``agg_slug`` variable. F340 fix neutralised."
    )
