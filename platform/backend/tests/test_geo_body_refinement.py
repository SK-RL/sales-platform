"""Body-scoped narrowing of remote_policy.

``classify_remote_policy`` reads only ``location_raw`` + ``remote_scope``.
On a live application run that proved dangerously permissive: 9 of 11
high-relevance candidates were geographically impossible for an
India-based candidate and every one was stored as worldwide or
unclassified. The restriction was in the job body every time —
TensorWave's location said "Remote" while the posting required
"authorization to work in the United States"; Cortex said "Remote" while
the body said "anywhere in the US"; Zscaler had no location and said
"This is a Hybrid role".

``refine_from_description`` reads the body for exactly those signals. It
is deliberately one-directional: it may only NARROW. The bug being fixed
is over-permissive classification, so a refinement that could widen would
reintroduce it from the other side.

Source-level, no DB.
"""
from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-geo")


# --- the real cases that motivated this ---------------------------------

def test_tensorwave_us_auth_in_boilerplate_is_not_worldwide():
    """Location field said "Remote"; the posting required US work
    authorisation in its legal text. Classified worldwide before."""
    from app.workers.tasks._role_matching import refine_from_description

    body = ("Employment is contingent upon verification of identity and "
            "authorization to work in the United States, as required by law.")
    assert refine_from_description("worldwide", [], body) == ("country_restricted", ["US"])


def test_cortex_anywhere_in_the_us_is_not_worldwide():
    from app.workers.tasks._role_matching import refine_from_description

    body = "We are remote and welcome candidates from anywhere in the US!"
    assert refine_from_description("worldwide", [], body) == ("country_restricted", ["US"])


def test_zscaler_hybrid_stated_only_in_body():
    from app.workers.tasks._role_matching import refine_from_description

    body = ("We are looking for a Senior DevOps Engineer. This is a Hybrid role, "
            "reporting to the Senior Manager. #LI-HYBRID")
    policy, _ = refine_from_description("unknown", [], body)
    assert policy == "hybrid"


# --- one-directional: may narrow, never widen ---------------------------

def test_never_widens_a_restricted_job_to_worldwide():
    """The whole point. A genuinely restricted job must survive intact."""
    from app.workers.tasks._role_matching import refine_from_description

    body = "Work from anywhere in the world, fully remote, no offices."
    assert refine_from_description("country_restricted", ["DE"], body) == (
        "country_restricted", ["DE"],
    )


def test_onsite_is_left_alone():
    """Already the most restrictive value; nothing to tighten."""
    from app.workers.tasks._role_matching import refine_from_description

    assert refine_from_description("onsite", [], "authorization to work in the United States") == (
        "onsite", [],
    )


def test_existing_non_us_restriction_is_not_overwritten_with_us():
    """A posting can mention US authorisation while being scoped elsewhere.
    Keep the narrower, already-correct answer."""
    from app.workers.tasks._role_matching import refine_from_description

    body = "Applicants must have authorization to work in the United States for our US entity."
    assert refine_from_description("country_restricted", ["AU"], body) == (
        "country_restricted", ["AU"],
    )


# --- no-ops -------------------------------------------------------------

def test_no_description_is_a_noop():
    from app.workers.tasks._role_matching import refine_from_description

    assert refine_from_description("worldwide", [], "") == ("worldwide", [])


def test_genuinely_worldwide_body_stays_worldwide():
    """Supabase: "We hire globally ... there are no offices." Must survive."""
    from app.workers.tasks._role_matching import refine_from_description

    body = ("We hire globally. We believe you can do your best work from anywhere. "
            "There are no Supabase offices.")
    assert refine_from_description("worldwide", [], body) == ("worldwide", [])


def test_returns_a_copy_not_the_caller_list():
    from app.workers.tasks._role_matching import refine_from_description

    countries = ["DE"]
    _, out = refine_from_description("country_restricted", countries, "")
    out.append("XX")
    assert countries == ["DE"]


# --- wired into both classification paths -------------------------------

def test_scan_task_applies_the_refinement():
    import inspect
    from app.workers.tasks import scan_task

    src = inspect.getsource(scan_task)
    assert "refine_from_description(" in src, "new jobs would still be misclassified"


def test_maintenance_task_applies_the_refinement():
    """This is the path that re-heals the existing corpus."""
    import inspect
    from app.workers.tasks import maintenance_task

    src = inspect.getsource(maintenance_task)
    assert "refine_from_description(" in src
