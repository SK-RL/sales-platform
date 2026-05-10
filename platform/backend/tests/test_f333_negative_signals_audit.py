"""F333 — tighten infra/security negative-title signals based on
2026-05-09 prod audit.

The audit pulled the 193 currently-relevant infra+global_remote
jobs from prod and ran them through a title-only false-positive
heuristic. 7 of 193 (4%) were classified into ``role_cluster=
infra`` despite their actual scope being teaching, project
management, frontend, or risk-analyst work:

  score=80 Udacity         "Cloud Technical Mentor — Independent Contractor"
  score=73 Correlation One "Lead Instructor: Data Center Technician"
  score=71 TensorWave      "Data Center Sr. Project Manager"
  score=71 TensorWave      "Data Center Project Engineer"
  score=70 Marbis          "Frontend DevOps Engineer"
  score=57 Dutchie         "Staff Platform Engineer — Frontend"
  score=57 Bitfinex        "Junior Risk Monitoring Analyst"

The keyword scorer fires on ``cloud / devops / platform engineer
/ data center`` etc. but the actual day-to-day role isn't infra
IC work. F333 adds these patterns to both
``_INFRA_NEGATIVE_TITLE_SIGNALS`` and (for parity, since the same
drift class affects security titles) the
``_SECURITY_NEGATIVE_TITLE_SIGNALS`` set.

Categories added:
  * Teaching / mentoring: ``instructor``, ``mentor``,
    ``curriculum developer``, ``training specialist``
  * Project management (non-IC): ``project manager``,
    ``program manager``, ``project engineer``
  * Frontend (non-infra scope): ``frontend``, ``front-end``,
    ``front end developer``
  * Risk / compliance analyst (finance/governance, not infra):
    ``risk monitoring``, ``risk analyst``, ``risk operations``,
    ``compliance analyst``
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
os.environ.setdefault("JWT_SECRET", "pytest-f333")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


_AUDIT_FALSE_POSITIVES_INFRA = [
    # (title as observed on prod, expected_to_no_longer_match_infra)
    "Cloud Technical Mentor - Independent Contractor (US Canada, AS Pacific Time Zones)",
    "Lead Instructor: Data Center Technician (Data Center Technician 8)",
    "Data Center Sr. Project Manager",
    "Data Center Project Engineer",
    "Frontend DevOps Engineer - Full or Part-time (m/f/x)",
    "Staff Platform Engineer - Frontend",
    "Junior Risk Monitoring Analyst (100% Remote - CET Timezone)",
]


def _import_helpers():
    """Import the modules under test.

    Wrapped in a function so the env defaults at module top-level
    are set BEFORE the app modules import config / database stuff.
    """
    from app.workers.tasks._role_matching import (
        _INFRA_NEGATIVE_TITLE_SIGNALS,
        _SECURITY_NEGATIVE_TITLE_SIGNALS,
        _title_has_signal,
    )
    return (
        _INFRA_NEGATIVE_TITLE_SIGNALS,
        _SECURITY_NEGATIVE_TITLE_SIGNALS,
        _title_has_signal,
    )


def test_infra_negatives_block_audit_false_positives():
    """Each title from the prod audit must now trigger the infra
    negative-signal check. Pre-fix, all 7 sailed past and ended
    up scored / cluster-tagged as ``infra``.
    """
    infra_neg, _, has_signal = _import_helpers()
    failures = []
    for title in _AUDIT_FALSE_POSITIVES_INFRA:
        norm = title.lower()
        if not has_signal(norm, infra_neg):
            failures.append(title)
    assert not failures, (
        f"F333 regression: these audit-flagged titles no longer "
        f"trigger _INFRA_NEGATIVE_TITLE_SIGNALS: {failures}"
    )


def test_security_negatives_match_infra_for_new_categories():
    """F333 added the new categories to the security negative-list
    in parity with infra. Spot-check that the same titles also
    trigger the security guard — useful when a JD mentions cyber
    keywords but the title is a non-infra role.
    """
    _, sec_neg, has_signal = _import_helpers()
    # Sample one title per category (4 total).
    samples = [
        "Cloud Technical Mentor - Independent Contractor",   # mentor
        "Lead Instructor: Cybersecurity Bootcamp",           # instructor
        "Cybersecurity Project Manager",                     # project manager
        "Security Compliance Analyst",                       # compliance analyst
    ]
    failures = [t for t in samples if not has_signal(t.lower(), sec_neg)]
    assert not failures, (
        f"F333 regression: security cluster lost parity with "
        f"infra on the new categories. Titles still passing the "
        f"security negative filter: {failures}"
    )


def test_genuine_infra_titles_still_pass():
    """Anti-regression — make sure the new tokens don't false-
    REJECT real infra/devops/SRE titles. ``project`` (without
    ``manager`` / ``engineer``) is fine, ``front`` (without the
    rest) is fine, etc.
    """
    infra_neg, _, has_signal = _import_helpers()
    legit_titles = [
        "Senior DevOps Engineer",
        "Site Reliability Engineer",
        "Staff Platform Engineer",       # unqualified — not "Frontend"
        "Cloud Infrastructure Engineer",
        "Senior Cloud Engineer",
        "Kubernetes Platform Engineer",
        "Infrastructure Lead",
        "Engineering Manager, Infrastructure",   # NOT "Project Manager"
        "Director of Engineering, Cloud",
    ]
    blocked = [t for t in legit_titles if has_signal(t.lower(), infra_neg)]
    assert not blocked, (
        f"F333 over-correction: legitimate infra titles now blocked "
        f"by negative-signal: {blocked}. Tighten the offending "
        f"token to a more specific phrase."
    )


def test_each_new_category_has_at_least_one_token():
    """Source-level guard: a future cleanup that drops one of the
    F333 tokens (without replacement) would silently regress
    coverage on that category.
    """
    infra_neg, sec_neg, _ = _import_helpers()
    required_per_category = {
        "teaching": ("instructor", "mentor"),
        "project_mgmt": ("project manager", "project engineer"),
        "frontend": ("frontend", "front-end"),
        "risk_analyst": ("risk monitoring", "risk analyst"),
    }
    for cat, tokens in required_per_category.items():
        missing_infra = [t for t in tokens if t not in infra_neg]
        missing_sec = [t for t in tokens if t not in sec_neg]
        assert not missing_infra, (
            f"F333 regression: infra negative list missing "
            f"{cat!r} tokens {missing_infra}"
        )
        assert not missing_sec, (
            f"F333 regression: security negative list missing "
            f"{cat!r} tokens {missing_sec}"
        )


def test_data_center_project_titles_specifically_blocked():
    """The two TensorWave titles were the most surprising audit
    hits because ``Data Center`` is a strong infra signal. Pin a
    test against the EXACT prod titles so any future relaxation
    has to re-justify these specific cases.
    """
    infra_neg, _, has_signal = _import_helpers()
    for title in [
        "Data Center Project Engineer",
        "Data Center Sr. Project Manager",
    ]:
        assert has_signal(title.lower(), infra_neg), (
            f"F333 regression: {title!r} no longer blocked. "
            f"TensorWave-class project-management roles will "
            f"silently re-enter the infra cluster."
        )
