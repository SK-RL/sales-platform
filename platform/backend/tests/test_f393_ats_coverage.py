"""F393 — the per-ATS guide is derived from the registries, so the docs
can never promise more than the code does."""

from app.fetchers.questions import SUPPORTED_QUESTION_PLATFORMS
from app.services.ats_coverage import ats_coverage, catalogued_platforms
from app.services.submitters import auto_submittable_platforms
from tests._routes import registered_paths


def test_every_capable_platform_is_documented():
    cat = catalogued_platforms()
    assert set(auto_submittable_platforms()) <= cat
    assert set(SUPPORTED_QUESTION_PLATFORMS) <= cat


def test_levels_come_from_registries_not_prose():
    rows = {r["platform"]: r for r in ats_coverage()}
    for p in auto_submittable_platforms():
        assert rows[p]["level"] == "automatic", p
    for p in set(SUPPORTED_QUESTION_PLATFORMS) - set(auto_submittable_platforms()):
        assert rows[p]["level"] == "review", p
    assert rows["zoho"]["level"] == "link" and any("CAPTCHA" in n for n in rows["zoho"]["notes"])
    assert rows["join"]["level"] == "closed" and rows["join"]["automatic"] == [] and rows["join"]["you"]


def test_rows_are_ordered_automatic_first_and_carry_action_items():
    rows = ats_coverage()
    levels = [r["level"] for r in rows]
    assert levels == sorted(levels, key={"automatic": 0, "review": 1, "link": 2, "closed": 3}.__getitem__)
    for r in rows:
        assert r["you"], f"{r['platform']} has no action item for the user"
        assert r["level_label"] and r["link_example"]


def test_endpoint_registered():
    assert "/api/v1/applications/ats-coverage" in registered_paths()
