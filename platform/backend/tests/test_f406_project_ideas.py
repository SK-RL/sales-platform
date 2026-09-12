"""F406 — "show your work": research → three cited project ideas → the
user picks. Stage 1 only; no project code is generated. What matters:
quotes are checked against the research (no company-specific claim on
the model's word), coverage numbers come from our corpus, and the
tasks/endpoints exist and never write code.
"""

import inspect
import json
from types import SimpleNamespace

from app.services import proof_project as pp


class TestTerms:
    def test_word_bounded_terms(self):
        t = pp.terms_in("We run Kubernetes on EKS, Terraform for IaC, Prometheus and Grafana; on-call with SLOs. Go services.")
        assert {"kubernetes", "terraform", "observability", "sre", "go"} <= t
        assert "go" not in pp.terms_in("We go to the office.")  # bare 'go' is not the language
        assert "azure" not in pp.terms_in("A pure delight")

    def test_every_term_compiles_and_has_a_label(self):
        assert all(isinstance(v[0], str) and v[0] for v in pp.SKILL_TERMS.values())
        assert len(pp._COMPILED) == len(pp.SKILL_TERMS)


class TestGrounding:
    def test_verbatim_and_near_verbatim_quotes_pass_paraphrase_fails(self):
        src = "We are treating the CI/CD platform as an internal product with a Golden Path for 150+ engineers."
        assert pp.quote_grounded("treating the CI/CD platform as an internal product", src)
        assert pp.quote_grounded("Treating the CI/CD platform as an internal product, with a golden path", src)
        assert not pp.quote_grounded("they want to migrate everything to Nomad next quarter", src)
        assert not pp.quote_grounded("CI", src)  # too short to mean anything

    def test_ideas_are_classified_by_where_their_grounded_quotes_come_from(self):
        research = {"sources": [
            {"id": "JD", "kind": "job_description", "title": "JD", "url": "", "text": "Own our Kubernetes platform and CI/CD pipelines"},
            {"id": "STATUS", "kind": "status_page", "title": "Status", "url": "https://status.x", "text": "- 2026-09-01 [major] Elevated API latency in eu-west"},
        ]}
        result = {"ideas": [
            {"id": "idea-1", "title": "Generic", "evidence": [{"source": "JD", "quote": "Own our Kubernetes platform"}]},
            {"id": "idea-2", "title": "Pain", "evidence": [{"source": "STATUS", "quote": "Elevated API latency in eu-west"}, {"source": "JD", "quote": "Kubernetes platform and CI/CD pipelines"}]},
            {"id": "idea-3", "title": "Made up", "evidence": [{"source": "BLOG", "quote": "we love Nomad"}, {"source": "JD", "quote": "migrating to Nomad"}]},
        ]}
        g = pp.ground_ideas(result, research)["ideas"]
        assert [i["title"] for i in g] == ["Pain", "Generic", "Made up"]
        assert g[0]["specificity"] == "company" and g[0]["grounded_evidence"] == 2
        assert g[1]["specificity"] == "role"
        assert g[2]["specificity"] == "ungrounded" and g[2]["grounded_evidence"] == 0
        assert g[2]["evidence"][0]["source_title"] == "(unknown source)"


class TestCoverage:
    def test_idea_coverage_needs_two_terms_unless_single(self):
        per_job = [["kubernetes", "terraform"], ["kubernetes"], ["python"], ["kubernetes", "observability", "terraform"]]
        assert pp.idea_coverage(["kubernetes", "terraform"], per_job) == 50.0
        assert pp.idea_coverage(["kubernetes"], per_job) == 75.0
        assert pp.idea_coverage([], per_job) == 0.0

    def test_corpus_coverage_reads_relevant_active_jobs(self):
        src = inspect.getsource(pp.corpus_term_coverage)
        assert '"infra", "security"' in src and "JobDescription" in src


class _Client:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.texts.pop(0))], stop_reason="end_turn", stop_details=None)


RESEARCH = {"company": "Acme", "job_title": "SRE", "sources": [
    {"id": "JD", "kind": "job_description", "title": "JD", "url": "", "text": "Own our Kubernetes platform and CI/CD pipelines"},
    {"id": "GITHUB", "kind": "github", "title": "GitHub acme", "url": "https://github.com/acme", "text": "- acme-operator (Go, 120 stars): Kubernetes operator for Acme deployments"},
], "found": {"github": True, "status": False, "blog": False, "errors": []}, "jd_terms": ["kubernetes", "cicd"]}


class TestGenerateIdeas:
    def test_three_ideas_with_ids_and_verdict(self):
        c = _Client(json.dumps({"ideas": [
            {"title": "Operator e2e harness", "angle": "initiative", "summary": "Test harness — for the operator.", "evidence": [{"source": "GITHUB", "quote": "Kubernetes operator for Acme deployments"}],
             "build": ["kind cluster", "envtest suite"], "deliverable": "repo + 2-min README", "skills_shown": ["Go", "Kubernetes"], "recipient_role": "Platform lead", "why_them": "owns the operator", "effort_hours": 8, "risks": "scope"},
            {"title": "B", "angle": "stack_match", "summary": "s", "evidence": [], "build": [], "deliverable": "", "skills_shown": [], "recipient_role": "", "effort_hours": 6},
            {"title": "C", "angle": "generic", "summary": "s", "evidence": [], "build": [], "deliverable": "", "skills_shown": [], "recipient_role": "", "effort_hours": 6},
            {"title": "D (dropped)", "angle": "generic"},
        ], "research_verdict": "GitHub shows an operator — strongest signal."}))
        r = pp.generate_ideas(RESEARCH, "résumé: Go, Kubernetes", client=c)
        assert [i["id"] for i in r["ideas"]] == ["idea-1", "idea-2", "idea-3"]
        assert "—" not in r["ideas"][0]["summary"] and "—" not in r["research_verdict"]
        prompt = c.calls[0]["messages"][0]["content"]
        assert "### SOURCE GITHUB" in prompt and "résumé: Go" in prompt
        g = pp.ground_ideas(r, RESEARCH)["ideas"]
        assert g[0]["specificity"] == "company" and g[1]["specificity"] == "ungrounded"

    def test_model_failure_is_reported_not_raised(self):
        class Boom:
            messages = SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("down")))

        assert pp.generate_ideas(RESEARCH, "r", client=Boom())["error"] == "idea generation failed"

    def test_system_prompt_demands_verbatim_quotes_and_no_private_access(self):
        s = pp._IDEAS_SYSTEM
        assert "verbatim" in s and "Never assume access to the company's private systems" in s and "4-16 hours" in s

    def test_generic_ideas_get_corpus_coverage_not_model_numbers(self):
        corpus = {"jobs": 4, "coverage": {k: 0.0 for k in pp.SKILL_TERMS}, "per_job": [["kubernetes", "terraform"], ["kubernetes", "observability"], ["python"], ["kubernetes", "terraform", "observability"]],
                  "computed_at": "t"}
        corpus["coverage"].update({"kubernetes": 75.0, "terraform": 50.0, "observability": 50.0})
        c = _Client(json.dumps({"ideas": [
            {"title": "K8s + Terraform lab", "terms": ["kubernetes", "terraform", "bogus"], "summary": "s", "build": ["a"], "deliverable": "d", "skills_shown": ["k"], "effort_hours": 8, "coverage_pct": 99},
            {"title": "Obs stack", "terms": ["observability"], "summary": "s", "build": ["a"], "deliverable": "d", "skills_shown": ["o"], "effort_hours": 8},
        ]}))
        r = pp.generate_generic_ideas(corpus, "résumé", client=c)
        by = {i["title"]: i for i in r["ideas"]}
        assert by["K8s + Terraform lab"]["terms"] == ["kubernetes", "terraform"] and by["K8s + Terraform lab"]["coverage_pct"] == 50.0
        assert by["Obs stack"]["coverage_pct"] == 50.0
        assert r["ideas"][0]["coverage_pct"] >= r["ideas"][1]["coverage_pct"] and r["jobs_in_corpus"] == 4


class TestResearchHelpers:
    def test_domain_and_slugs(self):
        co = SimpleNamespace(domain="", website="https://www.acme-corp.io/about", name="Acme Corp")
        assert pp._domain(co) == "acme-corp.io"
        assert pp._slugs(co) == ["acme-corp", "acmecorp"]

    def test_gather_research_without_network_has_jd_profile_and_openings(self):
        job = SimpleNamespace(id="j1", company_id="c1", title="SRE", url="https://x/jobs/1", description=None)
        other = SimpleNamespace(title="Platform Engineer", location_raw="Remote", description=SimpleNamespace(text_content="Kubernetes migration"))
        session = SimpleNamespace(execute=lambda q: SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [other])))
        co = SimpleNamespace(name="Acme", industry="Fintech", employee_count="120", headquarters="", funding_stage="Series B", total_funding="", website="https://acme.io",
                             tech_stack=["Kubernetes", "Go"], description="Payments infra.", domain="acme.io")
        r = pp.gather_research(session, job, co, fetch_external=False, job_description="Own the Kubernetes platform")
        ids = [s["id"] for s in r["sources"]]
        assert ids == ["JD", "PROFILE", "JOBS"]
        assert "Tech stack (from enrichment): Kubernetes, Go" in r["sources"][1]["text"]
        assert "Platform Engineer (Remote): Kubernetes migration" in r["sources"][2]["text"]
        assert r["jd_terms"] == ["kubernetes"] and r["found"] == {"github": False, "status": False, "blog": False, "errors": []}

    def test_github_org_only_accepted_when_it_points_back_at_the_company(self):
        class R:
            def __init__(self, code, data):
                self.status_code, self._d = code, data

            def json(self):
                return self._d

        class C:
            def get(self, url, **k):
                if url.endswith("/orgs/acme"):
                    return R(200, {"login": "acme", "blog": "https://unrelated.example", "html_url": "https://github.com/acme", "public_repos": 3})
                if url.endswith("/orgs/acmecorp"):
                    return R(200, {"login": "acmecorp", "blog": "https://acme-corp.io", "html_url": "https://github.com/acmecorp", "public_repos": 3})
                if url.endswith("/orgs/acmecorp/repos"):
                    return R(200, [{"name": "operator", "description": "K8s operator", "language": "Go", "stargazers_count": 10, "pushed_at": "2026-09-01T00:00:00Z", "topics": []},
                                   {"name": "old", "archived": True, "stargazers_count": 99}])
                return R(404, {})

        co = SimpleNamespace(domain="acme-corp.io", website="", name="Acme Corp")
        gh = pp._github_org(C(), co)
        assert gh["login"] == "acmecorp" and [r["name"] for r in gh["repos"]] == ["operator"]


def test_tasks_registered_with_limits_and_no_code_generation():
    from app.workers import tasks
    from app.workers.tasks import proof_task

    for name in ("project_ideas_task", "generic_project_ideas_task", "project_ideas_eval_task"):
        assert name in tasks.__all__
        t = getattr(proof_task, name)
        assert t.acks_late is False and t.soft_time_limit < t.time_limit
    src = inspect.getsource(proof_task)
    assert "generate_code" not in src and "subprocess" not in src
    assert "ensure_description_sync" in inspect.getsource(proof_task._run_one)


def test_endpoints_exist_and_choose_validates_idea():
    from app.api.v1 import applications

    for name in ("get_project_ideas", "draft_project_ideas", "choose_project_idea", "project_ideas_library", "run_project_ideas_eval", "get_project_ideas_eval"):
        assert hasattr(applications, name)
    src = inspect.getsource(applications.choose_project_idea)
    assert "Idea not found" in src and "project_idea_chosen" in src
    # library and eval routes are declared before the /{app_id} routes that could shadow them
    whole = inspect.getsource(applications)
    assert whole.index('"/project-ideas-library"') < whole.index('"/{app_id}/project-ideas"')
