"""F342 — ``top_missing_keywords`` filtered to resume's dominant cluster.

User feedback 2026-05-06 (in-app, #1, status=open):

  "In resume score section, while matching the ATS score it is
  not giving missing keywords suggestion from the uploaded
  resume. I uploaded resume for QA but it is giving non
  relevant suggestions. It should give the suggestions as per
  the QA roles not for devops or SRE."

Pre-fix the aggregation in ``api/v1/resume.py::list_scores``
walked EVERY ResumeScore row for the resume regardless of the
matched job's role cluster. Since the DB is dominated by infra
jobs (~67k Himalayas + large infra-leaning ATS pool), a QA
resume's scores were dominated by low-scoring infra-cluster
jobs; the top "missing keywords" then became
kubernetes/terraform/aws — the OPPOSITE of what a QA candidate
needs to see in their suggestion list.

F342 detects the resume's DOMINANT cluster (the cluster where
the resume scores highest on average) and aggregates missing
keywords ONLY from that cluster's jobs. Falls back to the
legacy all-clusters aggregation when there's no clear winner
(e.g. brand-new resume scored against an unclassified pool),
so the fix only kicks in when we can confidently identify the
resume's cluster.

Confidence threshold: dominant cluster needs ≥10 scored matches
AND a mean score that beats every other cluster by ≥5 points.

The response also gains a new ``missing_keywords_cluster`` field
so the FE can render an explanation ("showing keywords for QA
jobs based on your resume").
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
os.environ.setdefault("JWT_SECRET", "pytest-f342")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "resume.py").read_text()


def test_handler_computes_per_cluster_stats():
    """The handler must GROUP BY Job.role_cluster to detect the
    resume's dominant cluster. A future refactor that drops the
    grouping would silently revert to the all-clusters aggregation.
    """
    src = _read_handler()
    # Anchor on the missing-keyword aggregation block.
    block_idx = src.find("F342 regression fix")
    assert block_idx > 0, "F342 anchor comment missing"
    window = src[block_idx:block_idx + 4500]
    # Must group by role_cluster + collect counts and avg scores.
    assert "Job.role_cluster" in window
    assert "group_by(Job.role_cluster)" in window
    assert "func.count(ResumeScore.id)" in window
    assert "func.avg(ResumeScore.overall_score)" in window


def test_handler_applies_confidence_thresholds():
    """The dominant-cluster pick should require ≥10 matches AND
    a ≥5-point average-score gap over runners-up. Without those
    thresholds a brand-new resume with 2 incidental high scores
    on infra jobs would get its keywords incorrectly filtered.
    """
    src = _read_handler()
    block_idx = src.find("F342 regression fix")
    window = src[block_idx:block_idx + 4500]
    # Source-code presence of the two thresholds.
    assert "10" in window, "match-count threshold missing"
    assert "5.0" in window or "5 " in window, "score-gap threshold missing"


def test_handler_falls_back_to_legacy_aggregation_when_no_dominant_cluster():
    """If no cluster is dominant, the legacy all-clusters
    aggregation must still fire so brand-new resumes get
    SOMETHING back instead of an empty list.
    """
    src = _read_handler()
    block_idx = src.find("F342 regression fix")
    window = src[block_idx:block_idx + 4500]
    # Must have the ``else`` branch that does the legacy
    # all-clusters select.
    assert "if dominant_cluster:" in window
    assert "else:" in window
    # And the legacy branch select doesn't join Job.
    legacy_idx = window.find("Legacy aggregation across all clusters")
    assert legacy_idx > 0, (
        "F342 regression: legacy fallback branch removed or "
        "renamed. Brand-new resumes would now get an empty "
        "top_missing list when their cluster is ambiguous."
    )


def test_handler_emits_missing_keywords_cluster_field():
    """The FE needs to know WHICH cluster the suggestions are
    filtered to so it can render an explanation. The new
    ``missing_keywords_cluster`` field carries that info.
    """
    src = _read_handler()
    assert '"missing_keywords_cluster": dominant_cluster' in src, (
        "F342 regression: missing_keywords_cluster field no "
        "longer surfaces in the response. FE can't render the "
        "cluster context badge."
    )


def test_cluster_filtered_select_joins_job_table():
    """The cluster-filtered SELECT must JOIN Job so it can WHERE
    on role_cluster. A future copy-paste that drops the join
    would silently fall back to all-rows semantics with the
    cluster filter being a no-op.
    """
    src = _read_handler()
    block_idx = src.find("F342 regression fix")
    window = src[block_idx:block_idx + 4500]
    # The cluster-filtered branch must join Job and filter on
    # Job.role_cluster == dominant_cluster.
    cluster_branch = window[window.find("if dominant_cluster:"):]
    assert ".join(Job," in cluster_branch
    assert "Job.role_cluster == dominant_cluster" in cluster_branch


def test_cluster_filter_excludes_unclassified_jobs():
    """The dominant-cluster detection grouping must filter out
    NULL / empty role_cluster rows so unclassified jobs don't
    create a phantom "" cluster bucket.
    """
    src = _read_handler()
    block_idx = src.find("F342 regression fix")
    window = src[block_idx:block_idx + 4500]
    # The cluster-stats SELECT excludes nulls and empties.
    cluster_stats_section = window[window.find("cluster_stats_result"):]
    assert "Job.role_cluster.isnot(None)" in cluster_stats_section
    assert 'Job.role_cluster != ""' in cluster_stats_section
