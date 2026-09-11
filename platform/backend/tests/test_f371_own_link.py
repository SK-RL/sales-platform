"""F371 — bring your own link.

Tsenta accepts any URL and leaves Apply enabled even for a link it
understood nothing about. Ours either resolves the link to a posting on
an ATS we read — as a normal catalogue Job — or refuses with the reason
and creates nothing.
"""

import uuid

import pytest

import app.services.own_link as ol
from app.services.own_link import OwnLinkError, parse_job_url, refusal_for, resolve_job_from_url


class TestParse:
    @pytest.mark.parametrize("url,platform,slug,token,ext", [
        ("https://boards.greenhouse.io/figma/jobs/5426468004", "greenhouse", "figma", "5426468004", "5426468004"),
        ("https://job-boards.greenhouse.io/figma/jobs/5426468004?gh_src=x", "greenhouse", "figma", "5426468004", "5426468004"),
        ("https://boards.eu.greenhouse.io/acme/jobs/123", "greenhouse", "acme", "123", "123"),
        ("https://jobs.lever.co/matchgroup/7fca4a70-1234-4bcd-9abc-1234567890ab/apply", "lever", "matchgroup", "7fca4a70-1234-4bcd-9abc-1234567890ab", "7fca4a70-1234-4bcd-9abc-1234567890ab"),
        ("https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245/application", "ashby", "ramp", "34413f8d-26bf-4bbc-8ade-eb309a0e2245", "34413f8d-26bf-4bbc-8ade-eb309a0e2245"),
        ("https://jobs.ashbyhq.com/northflank.com/25024796-eeea-4b8b-bb49-b939ced37c65", "ashby", "northflank.com", "25024796-eeea-4b8b-bb49-b939ced37c65", "25024796-eeea-4b8b-bb49-b939ced37c65"),
        ("https://apply.workable.com/deeplight/j/86F4823B1B/apply/", "workable", "deeplight", "86F4823B1B", "86F4823B1B"),
        ("https://channable.recruitee.com/o/senior-devops-engineer", "recruitee", "channable", "senior-devops-engineer", None),
        ("https://icmarkets.bamboohr.com/careers/128/apply", "bamboohr", "icmarkets", "128", "bamboo-icmarkets-128"),
        ("https://jobs.smartrecruiters.com/Colliers/744000148671909-property-graduate", "smartrecruiters", "Colliers", "744000148671909", "sr-744000148671909"),
        # F376 — a Himalayas repost resolves to the repost row; the endpoint then finds the employer's form.
        ("https://himalayas.app/companies/bishop-fox/jobs/penetration-tester", "himalayas", "bishop-fox", "penetration-tester", "himalayas-penetration-tester"),
    ])
    def test_recognised_shapes(self, url, platform, slug, token, ext):
        p = parse_job_url(url)
        assert p is not None
        assert (p.platform, p.slug, p.token, p.external_id) == (platform, slug, token, ext)

    def test_scheme_is_optional(self):
        assert parse_job_url("boards.greenhouse.io/figma/jobs/1").platform == "greenhouse"

    @pytest.mark.parametrize("url", [
        "", "not a url", "https://example.com/careers", "https://www.bamboohr.com/",
        "https://jobs.lever.co/matchgroup", "https://boards.greenhouse.io/figma",
        "https://jobs.workable.com/view/abc", "https://acme.com/careers?gh_jid=123",
        "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1", "https://www.linkedin.com/jobs/view/123",
    ])
    def test_everything_else_is_refused(self, url):
        assert parse_job_url(url) is None

    def test_refusals_are_specific_where_we_can_be(self):
        assert "boards.greenhouse.io" in refusal_for("https://acme.com/careers?gh_jid=123")
        assert "apply.workable.com" in refusal_for("https://jobs.workable.com/view/abc")
        assert "account" in refusal_for("https://acme.wd5.myworkdayjobs.com/x")
        assert "Greenhouse, Lever, Ashby" in refusal_for("https://example.com/")


# ── resolver ─────────────────────────────────────────────────────

class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeSession:
    def __init__(self, jobs=(), boards=(), companies=()):
        self.jobs, self.boards, self.companies = list(jobs), list(boards), list(companies)
        self.added, self.committed, self.rolled_back, self.closed = [], False, False, False

    def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        name = getattr(entity, "__name__", "")
        rows = {"Job": self.jobs, "CompanyATSBoard": self.boards, "Company": self.companies}.get(name, [])
        return _Res(rows)

    def get(self, model, pk):
        for c in self.companies:
            if c.id == pk:
                return c
        return None

    def add(self, obj):
        self.added.append(obj)
        if obj.__class__.__name__ == "Company":
            self.companies.append(obj)
        if obj.__class__.__name__ == "CompanyATSBoard":
            self.boards.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def wired(monkeypatch):
    state = {"session": FakeSession(), "fetch": lambda platform, slug: [], "upserted": []}
    monkeypatch.setattr("app.workers.tasks._db.SyncSession", lambda: state["session"])
    monkeypatch.setattr(ol, "_fetch_board", lambda platform, slug: state["fetch"](platform, slug))
    state["fetch_one"] = lambda platform, slug, ext: None
    monkeypatch.setattr(ol, "_fetch_one", lambda platform, slug, ext: state["fetch_one"](platform, slug, ext))

    def fake_upsert(session, company, board, raw, **kw):
        state["upserted"].append(raw)
        session.jobs.append(Row(id="job-new", platform=board.platform, external_id=raw["external_id"],
                                title=raw["title"], url=raw["url"], company_id=company.id))
        return "new"

    monkeypatch.setattr("app.workers.tasks.scan_task._upsert_job", fake_upsert)
    return state


ASHBY = "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245"


class TestResolve:
    def test_unrecognised_link_creates_nothing(self, wired):
        with pytest.raises(OwnLinkError) as e:
            resolve_job_from_url("https://example.com/jobs/1")
        assert e.value.status == 422
        assert wired["session"].added == [] and wired["upserted"] == []

    def test_existing_job_is_returned_without_fetching(self, wired):
        co = Row(id="c1", name="Ramp")
        wired["session"] = FakeSession(
            jobs=[Row(id="j1", platform="ashby", external_id="34413f8d-26bf-4bbc-8ade-eb309a0e2245",
                      title="Security Engineer", url=ASHBY, company_id="c1")],
            companies=[co],
        )
        wired["fetch"] = lambda p, s: (_ for _ in ()).throw(AssertionError("must not fetch"))
        r = resolve_job_from_url(ASHBY)
        assert r.job_id == "j1" and r.created is False and r.company_name == "Ramp"

    def test_unknown_board_is_fetched_and_the_posting_upserted(self, wired):
        wired["fetch"] = lambda p, s: [
            {"external_id": "other", "title": "Other", "url": "https://jobs.ashbyhq.com/ramp/other", "company_name": "Ramp"},
            {"external_id": "34413f8d-26bf-4bbc-8ade-eb309a0e2245", "title": "Security Engineer, Cloud",
             "url": ASHBY, "company_name": "Ramp"},
        ]
        r = resolve_job_from_url(ASHBY)
        assert r.created is True and r.title == "Security Engineer, Cloud"
        assert [u["external_id"] for u in wired["upserted"]] == ["34413f8d-26bf-4bbc-8ade-eb309a0e2245"]
        kinds = [a.__class__.__name__ for a in wired["session"].added]
        assert "Company" in kinds and "CompanyATSBoard" in kinds
        assert wired["session"].committed

    def test_posting_missing_from_board_is_404(self, wired):
        wired["fetch"] = lambda p, s: [{"external_id": "other", "title": "Other", "url": "x"}]
        with pytest.raises(OwnLinkError) as e:
            resolve_job_from_url(ASHBY)
        assert e.value.status == 404 and "closed" in e.value.detail

    def test_unlisted_posting_is_resolved_through_fetch_one(self, wired):
        wired["fetch"] = lambda p, s: [{"external_id": "other", "title": "Other", "url": "x", "company_name": "Ramp"}]
        wired["fetch_one"] = lambda p, s, e: {"external_id": e, "title": "Security Engineer, Cloud", "url": ASHBY, "company_name": "Ramp"}
        r = resolve_job_from_url(ASHBY)
        assert r.created is True and r.title == "Security Engineer, Cloud"

    def test_empty_board_says_so(self, wired):
        with pytest.raises(OwnLinkError) as e:
            resolve_job_from_url(ASHBY)
        assert e.value.status == 404 and "no public postings" in e.value.detail

    def test_board_fetch_failure_is_502_not_500(self, wired):
        def boom(p, s):
            raise RuntimeError("timeout")
        wired["fetch"] = boom
        with pytest.raises(OwnLinkError) as e:
            resolve_job_from_url(ASHBY)
        assert e.value.status == 502

    def test_recruitee_matches_by_url_slug(self, wired):
        url = "https://channable.recruitee.com/o/senior-devops-engineer"
        wired["fetch"] = lambda p, s: [
            {"external_id": "recruitee-1", "title": "Other", "url": "https://channable.recruitee.com/o/other", "company_name": "Channable"},
            {"external_id": "recruitee-2", "title": "Senior DevOps Engineer", "url": url + "?src=x", "company_name": "Channable"},
        ]
        r = resolve_job_from_url(url)
        assert r.external_id == "recruitee-2"

    def test_session_is_always_closed(self, wired):
        with pytest.raises(OwnLinkError):
            resolve_job_from_url("https://example.com/")
        # unrecognised links never open a session; a fetch failure does
        def boom(p, s):
            raise RuntimeError("x")
        wired["fetch"] = boom
        with pytest.raises(OwnLinkError):
            resolve_job_from_url(ASHBY)
        assert wired["session"].closed and wired["session"].rolled_back


class TestEndpoint:
    def test_route_is_registered(self):
        from tests._routes import registered_paths
        assert "/api/v1/applications/from-url" in registered_paths()

    def test_endpoint_reports_capability(self):
        import inspect
        from app.api.v1 import applications
        src = inspect.getsource(applications.application_from_url)
        assert "auto_submittable_platforms()" in src and "human_wall_for(" in src


class TestRepostLinks:
    """F376 — pasting a Himalayas link goes through the resolver to the
    employer's form, or is refused with the reason."""

    def test_endpoint_routes_reposts_through_the_resolver(self):
        import inspect
        from app.api.v1 import applications
        src = inspect.getsource(applications.application_from_url)
        assert "AGGREGATOR_PLATFORMS" in src and "_resolve_repost" in src

    def test_unresolved_repost_is_refused_not_prepared(self, monkeypatch):
        from fastapi import HTTPException
        from app.api.v1 import applications
        from app.services.own_link import ResolvedJob

        class S:
            def get(self, model, pk):
                return Row(id=pk, title="DevSecOps Engineer", company_id=None)
            def close(self):
                pass
        monkeypatch.setattr("app.workers.tasks._db.SyncSession", lambda: S())
        monkeypatch.setattr("app.services.aggregator_resolver.resolve_job",
                            lambda session, row: {"resolved_job_id": None, "apply_platform": "workday"})
        r = ResolvedJob(str(uuid.uuid4()), "himalayas", "caci", "himalayas-x", "DevSecOps Engineer", "CACI", "https://himalayas.app/x", False)
        with pytest.raises(HTTPException) as e:
            applications._resolve_repost(r)
        assert e.value.status_code == 422
        assert "workday" in e.value.detail and "paste the employer" in e.value.detail

    def test_resolved_repost_returns_the_employers_job(self, monkeypatch):
        from app.api.v1 import applications
        from app.services.own_link import ResolvedJob

        real_id, repost_id, co_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        rows = {repost_id: Row(id=repost_id, title="Penetration Tester", company_id=co_id),
                real_id: Row(id=real_id, platform="greenhouse", external_id="1", title="Penetration Tester",
                             url="https://boards.greenhouse.io/bishopfox/jobs/1", company_id=co_id),
                co_id: Row(id=co_id, name="Bishop Fox")}
        class S:
            def get(self, model, pk):
                return rows.get(pk)
            def close(self):
                pass
        monkeypatch.setattr("app.workers.tasks._db.SyncSession", lambda: S())
        monkeypatch.setattr("app.services.aggregator_resolver.resolve_job",
                            lambda session, row: {"resolved_job_id": str(real_id), "apply_platform": "greenhouse"})
        out = applications._resolve_repost(ResolvedJob(str(repost_id), "himalayas", "bishop-fox", "himalayas-x", "Penetration Tester", "Bishop Fox", "https://himalayas.app/x", False))
        assert out.job_id == str(real_id) and out.platform == "greenhouse" and out.company_name == "Bishop Fox"


    def test_slow_resolution_is_bounded_with_a_retry_message(self):
        import inspect
        from app.api.v1 import applications
        src = inspect.getsource(applications.application_from_url)
        assert "asyncio.wait_for" in src and "REPOST_RESOLVE_BUDGET_S" in src and "504" in src
        assert applications.REPOST_RESOLVE_BUDGET_S < 60


    def test_unknown_himalayas_link_is_refused_plainly(self, wired):
        """Seen on production: a made-up Himalayas slug was told the posting
        'isn't on the board any more' — its API returns the global feed for
        an unknown company, so that wording was false."""
        wired["fetch"] = lambda p, s: [{"external_id": "himalayas-other", "title": "Other", "url": "https://himalayas.app/companies/x/jobs/other"}]
        with pytest.raises(OwnLinkError) as e:
            resolve_job_from_url("https://himalayas.app/companies/no-such-company/jobs/nothing-here")
        assert e.value.status == 404 and "Himalayas" in e.value.detail and "any more" not in e.value.detail


    def test_company_is_matched_by_slug_when_the_name_differs(self, wired):
        """Production: Personio greenbone-ag 500'd because a Himalayas scan
        had already created the company under another display name and
        companies.slug is unique."""
        existing = Row(id="c-existing", name="Greenbone Networks", slug="greenbone-ag")
        wired["session"] = FakeSession(companies=[existing])
        wired["fetch"] = lambda p, s: [{"external_id": "2546372", "title": "Account Manager", "url": "https://greenbone-ag.jobs.personio.com/job/2546372", "company_name": "Greenbone AG"}]
        r = resolve_job_from_url("https://greenbone-ag.jobs.personio.com/job/2546372")
        kinds = [a.__class__.__name__ for a in wired["session"].added]
        assert "Company" not in kinds and r.company_name == "Greenbone Networks"
