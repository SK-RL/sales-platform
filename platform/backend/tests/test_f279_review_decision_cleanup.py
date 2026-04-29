"""F279 — analytics review-insights divergence + legacy decision cleanup.

Closes F110 from the regression report:

    /analytics/overview.reviewed_count = 11    (only canonical past-tense)
    /analytics/review-insights.total_reviewed = 41    (sums ALL distinct
                                                       decisions including
                                                       legacy verb-forms)

Same prod data, two different numbers — broke trust in the
acceptance-rate signal that's the platform's primary review-loop
metric.

Two-part fix:
  (a) ``analytics.py::review_insights`` filters the GROUP BY to
      canonical past-tense decisions only, so future rows can't
      re-introduce the divergence.
  (b) ``app/cleanup_review_decisions.py`` backfills any legacy
      verb-form rows so the column has a single vocabulary.

These tests are source-level checks. The actual data backfill is
verified via the script's own logging (before / after distribution).
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
os.environ.setdefault("JWT_SECRET", "pytest-f279")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_analytics_review_insights_filters_to_canonical_decisions():
    """The GROUP BY in ``review_insights`` must filter to the
    canonical past-tense decisions (``accepted``/``rejected``/
    ``skipped``). Without the filter, legacy verb-form rows
    (``accept``/``reject``/``skip``) inflate ``total_reviewed``
    relative to ``/analytics/overview.reviewed_count``.
    """
    src = (_BACKEND / "app" / "api" / "v1" / "analytics.py").read_text()
    # Anchor on the review_insights handler.
    handler_start = src.find("def review_insights(")
    assert handler_start >= 0, (
        "F279 regression: review_insights handler missing from "
        "analytics.py — file structure changed."
    )
    handler_end = src.find("@router.", handler_start + 1)
    if handler_end < 0:
        handler_end = len(src)
    handler = src[handler_start:handler_end]
    assert "_CANONICAL_REVIEW_DECISIONS" in handler or (
        '"accepted"' in handler and '"rejected"' in handler
        and '"skipped"' in handler
    ), (
        "F279 regression: review_insights no longer references the "
        "canonical decision allow-list. Without filtering the "
        "GROUP BY to canonical values, legacy verb-form rows "
        "re-inflate total_reviewed."
    )
    # The defensive shape is a ``.where(Review.decision.in_(...))``
    # before the GROUP BY. Anchor on the in_( call near the GROUP BY.
    assert "Review.decision.in_(" in handler, (
        "F279 regression: review_insights no longer applies "
        "``.where(Review.decision.in_(...))`` before the GROUP BY. "
        "The endpoint now sums every distinct decision value, "
        "re-introducing the F110 divergence."
    )


def test_cleanup_script_exists_and_maps_legacy_to_canonical():
    """The ``cleanup_review_decisions.py`` companion script must
    exist and define the same legacy→canonical mapping that the
    review handler uses. Drift between the two means new legacy
    rows could appear that the cleanup script doesn't recognize.
    """
    script_path = _BACKEND / "app" / "cleanup_review_decisions.py"
    assert script_path.exists(), (
        "F279 regression: cleanup script missing. The forward path "
        "is fixed and analytics is defensive, but legacy DB rows "
        "still exist until this script runs."
    )
    src = script_path.read_text()
    # Must map all three verb-forms.
    assert '"accept"' in src and '"accepted"' in src, (
        "F279 regression: cleanup script doesn't map ``accept`` -> "
        "``accepted``. Legacy rows with that value won't be cleaned."
    )
    assert '"reject"' in src and '"rejected"' in src, (
        "F279 regression: cleanup script doesn't map ``reject`` -> "
        "``rejected``. Legacy rows with that value won't be cleaned."
    )
    assert '"skip"' in src and '"skipped"' in src, (
        "F279 regression: cleanup script doesn't map ``skip`` -> "
        "``skipped``. Legacy rows with that value won't be cleaned."
    )


def test_cleanup_script_supports_dry_run():
    """``--dry-run`` is the operator's safety net — they look at
    the ``before`` count, decide whether to commit. Removing it
    means the only way to see the impact is to run the write,
    which is exactly the kind of foot-gun this script was created
    to avoid.
    """
    script = (_BACKEND / "app" / "cleanup_review_decisions.py").read_text()
    assert "--dry-run" in script, (
        "F279 regression: cleanup script no longer supports "
        "``--dry-run``. Operators have no way to inspect impact "
        "before committing."
    )


def test_review_handler_decision_map_matches_cleanup_script():
    """The handler's normalization map and the cleanup script's
    backfill map must encode the same legacy→canonical contract.
    If a future contributor adds a fourth verb-form to one but not
    the other, the rows it produces won't be cleaned up.
    """
    handler_src = (_BACKEND / "app" / "api" / "v1" / "reviews.py").read_text()
    cleanup_src = (_BACKEND / "app" / "cleanup_review_decisions.py").read_text()
    # Both should reference the same three legacy keys. Use loose
    # substring checks to keep this test resilient to formatting.
    for verb, past in [("accept", "accepted"), ("reject", "rejected"), ("skip", "skipped")]:
        assert verb in handler_src and past in handler_src, (
            f"F279 regression: review handler no longer maps "
            f"{verb!r} -> {past!r}. The forward normalization is "
            f"part of the F73/F279 contract."
        )
        assert verb in cleanup_src and past in cleanup_src, (
            f"F279 regression: cleanup script no longer maps "
            f"{verb!r} -> {past!r}. Legacy rows with that value "
            f"won't be backfilled."
        )
