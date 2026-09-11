"""F374 — aggregator reposts resolve to the employer's real form.

Himalayas is the catalogue's largest source and every row is a repost.
The resolver follows the page's Apply redirect, fingerprints where it
lands, and — when that is an ATS we read — creates the catalogue Job
through the own-link path so apply uses the real form. Every outcome is
recorded (resolved / external / blocked / no_link / error) so coverage
is measured, not guessed.
"""

import inspect
import uuid

import pytest

import app.services.aggregator_resolver as ar
from app.services.aggregator_resolver import Resolution, find_apply_link, is_challenge, resolve_job
from app.services.own_link import OwnLinkError

PAGE = "https://himalayas.app/companies/acme/jobs/sre"

HTML = """
<html><body>
<header><a href="/employers">Post a job</a><a href="/signup">Apply now — get hired</a></header>
<nav><a href="/jobs/apply-tips">How to apply</a></nav>
<main>
  <h1>Site Reliability Engineer</h1>
  <a class="btn" href="/companies/acme/jobs/sre/apply">Apply now</a>
  <a href="https://acme.com/careers">Company site</a>
</main>
<footer><a href="/about">About</a></footer>
</body></html>
"""


class TestFindApplyLink:
    def test_picks_the_apply_button_not_nav_or_footer(self):
        link, cands = find_apply_link(HTML, PAGE)
        assert link == "https://himalayas.app/companies/acme/jobs/sre/apply"
        assert all("employers" not in c and "signup" not in c for c in cands)

    def test_prefers_an_off_site_apply_anchor(self):
        html = HTML.replace('<a class="btn" href="/companies/acme/jobs/sre/apply">Apply now</a>',
                            '<a href="/companies/acme/jobs/sre/apply">Apply now</a>'
                            '<a href="https://boards.greenhouse.io/acme/jobs/1">Apply on Greenhouse</a>')
        link, _ = find_apply_link(html, PAGE)
        assert link == "https://boards.greenhouse.io/acme/jobs/1"

    def test_href_only_fallback(self):
        html = '<main><a href="/x/apply?src=1"><img alt=""></a></main>'
        link, _ = find_apply_link(html, PAGE)
        assert link == "https://himalayas.app/x/apply?src=1"

    def test_nothing_is_none_with_candidates_empty(self):
        assert find_apply_link("<main><a href='/jobs'>Browse</a></main>", PAGE) == (None, [])


class TestChallenge:
    def test_cloudflare_interstitial(self):
        assert is_challenge(403, "<title>Just a moment...</title>")

    def test_plain_404_is_not_a_challenge(self):
        assert not is_challenge(404, "<h1>Not found</h1>")

    def test_ok_page_mentioning_challenge_is_not(self):
        assert not is_challenge(200, "just a moment while we load")


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeSession:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


def _job():
    # remoteok, not himalayas: F376b skips the page for aggregators known
    # to challenge every server fetch, and these tests exercise the page path.
    return Row(id=uuid.uuid4(), url=PAGE, platform="remoteok", title="SRE", company_id=None, raw_json={},
               apply_url=None, apply_platform=None, resolved_job_id=None, apply_resolve_status=None, apply_resolved_at=None)


class TestResolveJob:
    def test_resolved_links_the_catalogue_job(self, monkeypatch):
        monkeypatch.setattr(ar, "resolve_page", lambda url: Resolution("resolved", "https://boards.greenhouse.io/acme/jobs/1", "greenhouse"))
        linked = Row(job_id=str(uuid.uuid4()))
        monkeypatch.setattr("app.services.own_link.resolve_job_from_url", lambda url: linked)
        job, s = _job(), FakeSession()
        out = resolve_job(s, job)
        assert out["status"] == "resolved" and out["resolved_job_id"] == linked.job_id
        assert job.apply_platform == "greenhouse" and job.apply_resolved_at is not None and s.committed

    def test_posting_gone_from_board_is_external_not_resolved(self, monkeypatch):
        monkeypatch.setattr(ar, "resolve_page", lambda url: Resolution("resolved", "https://boards.greenhouse.io/acme/jobs/1", "greenhouse"))
        def gone(url):
            raise OwnLinkError(404, "closed")
        monkeypatch.setattr("app.services.own_link.resolve_job_from_url", gone)
        job = _job()
        out = resolve_job(job, FakeSession()) if False else resolve_job(FakeSession(), job)
        assert out["status"] == "external" and out["resolved_job_id"] is None
        assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/1"

    def test_external_keeps_the_employer_link(self, monkeypatch):
        monkeypatch.setattr(ar, "resolve_page", lambda url: Resolution("external", "https://acme.wd5.myworkdayjobs.com/x", "workday"))
        job = _job()
        out = resolve_job(FakeSession(), job)
        assert out["status"] == "external" and job.apply_platform == "workday" and job.resolved_job_id is None

    def test_blocked_page_with_nothing_to_look_up_is_unmatched(self, monkeypatch):
        """F375: a walled page falls through to the company lookup; with
        no company it ends `unmatched`, recorded, not raised."""
        monkeypatch.setattr(ar, "resolve_page", lambda url: Resolution("blocked", detail="challenged"))
        job = _job()
        job.company_id, job.title, job.raw_json = None, "SRE", {}
        assert resolve_job(FakeSession(), job)["status"] == "unmatched"
        assert job.apply_resolved_at is not None  # not retried forever


class TestWiring:
    def test_task_registered_and_scheduled(self):
        from app.workers.celery_app import celery_app
        from app.workers.tasks.aggregator_task import resolve_aggregator_links
        assert resolve_aggregator_links.name in celery_app.tasks
        assert "resolve_aggregator_links" in celery_app.conf.beat_schedule

    def test_endpoint_registered(self):
        from tests._routes import registered_paths
        assert "/api/v1/jobs/{job_id}/resolve-apply-link" in registered_paths()

    def test_prepare_and_readiness_use_the_resolved_job(self):
        from app.api.v1 import applications
        assert "job.resolved_job_id" in inspect.getsource(applications.prepare_application)
        assert "job.resolved_job_id" in inspect.getsource(applications.get_apply_readiness)

    def test_aggregators_are_never_auto_applied_directly(self):
        from app.services.submitters import auto_submittable_platforms
        assert not (ar.AGGREGATOR_PLATFORMS & auto_submittable_platforms())

    def test_single_alembic_head(self):
        import glob, os, re
        revs, downs = {}, set()
        for f in glob.glob(os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "*.py")):
            s = open(f).read()
            r = re.search(r"^revision\s*=\s*['\"]([^'\"]+)", s, re.M)
            d = re.search(r"^down_revision\s*=\s*['\"]([^'\"]+)", s, re.M)
            if r: revs[r.group(1)] = f
            if d: downs.add(d.group(1))
        heads = [k for k in revs if k not in downs]
        assert heads == ["t7u8v9w0x1y2"], heads


class TestCircuitBreaker:
    def test_a_run_stops_after_consecutive_blocks(self, monkeypatch):
        """Production: Himalayas challenges the VM on every page. Don't
        keep knocking; leave the rest unresolved for another day."""
        import app.workers.tasks.aggregator_task as agt

        jobs = [_job() for _ in range(8)]

        class _Res:
            def scalars(self): return self
            def all(self): return jobs

        class _S:
            def execute(self, stmt): return _Res()
            def rollback(self): pass
            def close(self): pass

        monkeypatch.setattr(agt, "SyncSession", lambda: _S())
        monkeypatch.setattr(agt, "PAUSE_SECONDS", 0)
        calls = []
        def fake_resolve(session, job, skip_page=False):
            calls.append(skip_page)
            return {"status": "unmatched", "detail": "aggregator challenged the fetch (HTTP 403)"}
        monkeypatch.setattr(agt, "resolve_job", fake_resolve)
        out = agt.resolve_aggregator_links()
        # every job still gets the company lookup (F375) …
        assert len(calls) == 8 and out["unmatched"] == 8
        # … but page fetches stop after three challenges in a row
        assert calls == [False] * agt.BLOCKED_STREAK_STOP + [True] * 5


class TestOnDemandIsQueued:
    def test_endpoint_enqueues_and_answers_202(self):
        import inspect
        from app.api.v1 import jobs as jobs_api
        src = inspect.getsource(jobs_api.resolve_apply_link)
        assert "resolve_one_aggregator_job.delay" in src and "202" in src

    def test_resolver_records_what_happened_on_the_row(self, monkeypatch):
        monkeypatch.setattr(ar, "resolve_page", lambda url: Resolution("no_link", detail="no apply anchor"))
        monkeypatch.setattr("app.services.company_lookup.lookup", lambda s, j, t=None: None)
        job = _job()
        resolve_job(FakeSession(), job)
        assert job.raw_json["apply_resolve"]["status"] == "no_link" and "timings" in job.raw_json["apply_resolve"]
