"""F375 — find a repost's real posting by company + title.

Himalayas walls its pages (F374), but the company and title on the
repost are enough: the ATSes' public APIs answer directly. Verified
live: Bishop Fox → greenhouse/bishopfox "Penetration Tester";
Supabase → ashby/supabase "Site Reliability Engineer"; Ramp →
ashby/ramp. A missing board is an empty list on every fetcher.
"""

import uuid

import pytest

import app.services.aggregator_resolver as ar
import app.services.company_lookup as cl
from app.services.aggregator_resolver import Resolution, resolve_job
from app.services.company_lookup import normalise_company, probe_boards, slug_candidates, titles_match


class TestSlugs:
    def test_suffixes_are_dropped(self):
        assert slug_candidates("CACI International Inc")[:2] == ["caciinternational", "caci-international"]
        assert "ewor" in slug_candidates("EWOR GmbH")

    def test_single_word(self):
        assert slug_candidates("Ramp") == ["ramp"]

    def test_lone_first_word_is_never_a_candidate(self):
        """'general' would be someone else's board."""
        assert "general" not in slug_candidates("General Dynamics Information Technology")
        assert "bishop" not in slug_candidates("Bishop Fox")

    def test_empty(self):
        assert slug_candidates("") == []


class TestTitles:
    def test_noise_is_ignored(self):
        assert titles_match("Mobility Cloud Engineer (100 % remote) (m/f/d)", "Mobility Cloud Engineer")
        assert titles_match(" Security Engineer, Cloud", "Security Engineer, Cloud")

    def test_generic_containment_does_not_match(self):
        assert not titles_match("Engineer", "Software Engineer")
        # a different, more senior role — must not match (own-link would apply to it)
        assert not titles_match("Site Reliability Engineer", "Senior Site Reliability Engineer - Vulnerability")

    def test_long_containment_matches(self):
        assert titles_match("Site Reliability Engineer Platform", "Site Reliability Engineer, Platform (Remote)")


class TestProbe:
    def test_first_board_with_the_title_wins(self):
        seen = []
        def fetch(p, s):
            seen.append((p, s))
            return [{"title": "Penetration Tester", "url": "https://boards.greenhouse.io/bishopfox/jobs/1"}] if (p, s) == ("greenhouse", "bishopfox") else []
        r = probe_boards("Bishop Fox", "Penetration Tester", fetch=fetch)
        assert r[0] == "greenhouse" and r[1] == "bishopfox"

    def test_existing_board_without_the_title_stops_the_probe(self):
        calls = []
        def fetch(p, s):
            calls.append((p, s))
            return [{"title": "Other role", "url": "x"}] if (p, s) == ("greenhouse", "acme") else []
        assert probe_boards("Acme", "SRE", fetch=fetch) is None
        assert calls[-1] == ("greenhouse", "acme")  # did not go on to ashby/acme etc.

    def test_no_board_anywhere(self):
        assert probe_boards("Nobody Corp", "SRE", fetch=lambda p, s: []) is None

    def test_fetch_errors_are_skipped(self):
        def fetch(p, s):
            raise RuntimeError("boom")
        assert probe_boards("Acme", "SRE", fetch=fetch) is None


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeSession:
    def __init__(self, company=None):
        self.company, self.committed = company, False
    def get(self, model, pk):
        return self.company
    def commit(self):
        self.committed = True


def _job():
    return Row(id=uuid.uuid4(), url="https://himalayas.app/x", platform="himalayas", title="Penetration Tester",
               company_id=uuid.uuid4(), raw_json={}, apply_url=None, apply_platform=None,
               resolved_job_id=None, apply_resolve_status=None, apply_resolved_at=None)


class TestResolverFallsThroughToLookup:
    def test_walled_page_then_company_match_is_resolved(self, monkeypatch):
        monkeypatch.setattr(ar, "resolve_page", lambda url: Resolution("blocked", detail="aggregator challenged the fetch (HTTP 403)"))
        linked = str(uuid.uuid4())
        monkeypatch.setattr(cl, "find_in_catalogue", lambda s, n, t: None)
        monkeypatch.setattr(cl, "probe_boards", lambda n, t, fetch=None: ("greenhouse", "bishopfox", {"title": t, "url": "https://boards.greenhouse.io/bishopfox/jobs/1"}))
        monkeypatch.setattr("app.services.own_link.resolve_job_from_url", lambda url: Row(job_id=linked))
        job = _job()
        out = resolve_job(FakeSession(Row(name="Bishop Fox", website="")), job)
        assert out["status"] == "resolved" and out["resolved_job_id"] == linked
        assert job.apply_platform == "greenhouse" and "company + title" in out["detail"]

    def test_walled_page_and_no_match_is_unmatched(self, monkeypatch):
        monkeypatch.setattr(ar, "resolve_page", lambda url: Resolution("blocked", detail="aggregator challenged the fetch (HTTP 403)"))
        monkeypatch.setattr(cl, "find_in_catalogue", lambda s, n, t: None)
        monkeypatch.setattr(cl, "probe_boards", lambda n, t, fetch=None: None)
        monkeypatch.setattr(cl, "fingerprint_company", lambda s, cid: [])
        job = _job()
        assert resolve_job(FakeSession(Row(name="Nobody", website="")), job)["status"] == "unmatched"

    def test_skip_page_still_runs_the_lookup(self, monkeypatch):
        called = {"page": 0}
        def page(url):
            called["page"] += 1
            return Resolution("blocked")
        monkeypatch.setattr(ar, "resolve_page", page)
        monkeypatch.setattr(cl, "find_in_catalogue", lambda s, n, t: Row(id=uuid.uuid4(), platform="ashby", url="https://jobs.ashbyhq.com/x/y"))
        job = _job()
        out = resolve_job(FakeSession(Row(name="X", website="")), job, skip_page=True)
        assert called["page"] == 0 and out["status"] == "resolved"

    def test_catalogue_match_needs_no_network(self, monkeypatch):
        monkeypatch.setattr(ar, "resolve_page", lambda url: Resolution("blocked"))
        hit = Row(id=uuid.uuid4(), platform="greenhouse", url="https://boards.greenhouse.io/a/jobs/1")
        monkeypatch.setattr(cl, "find_in_catalogue", lambda s, n, t: hit)
        def no(*a, **k):
            raise AssertionError("must not probe")
        monkeypatch.setattr(cl, "probe_boards", no)
        job = _job()
        assert resolve_job(FakeSession(Row(name="A", website="")), job)["resolved_job_id"] == str(hit.id)


class TestNormalise:
    def test_company(self):
        assert normalise_company("The Acme Corp.") == "acme"
        assert normalise_company("Bishop Fox") == "bishop fox"


class TestCanonicalUrl:
    """Bishop Fox, live: the Greenhouse API returned the company's embedded
    page (bishopfox.com/jobs?gh_jid=…), which own-link refuses. The probe
    knows slug + id, so the hosted URL is built directly."""

    def test_greenhouse_embedded_url_is_replaced(self):
        from app.services.company_lookup import canonical_posting_url
        assert canonical_posting_url("greenhouse", "bishopfox", {"external_id": "7905132", "url": "http://www.bishopfox.com/jobs?gh_jid=7905132"}) == \
            "https://boards.greenhouse.io/bishopfox/jobs/7905132"

    def test_other_platforms(self):
        from app.services.company_lookup import canonical_posting_url
        assert canonical_posting_url("ashby", "ramp", {"external_id": "abc"}) == "https://jobs.ashbyhq.com/ramp/abc"
        assert canonical_posting_url("recruitee", "x", {"external_id": "recruitee-1", "url": "https://x.recruitee.com/o/y"}) == "https://x.recruitee.com/o/y"
