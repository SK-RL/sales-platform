"""F404 — outreach to hiring contacts + email verification that works
without port 25. The user chose Tsenta's Networking features 1 and 2:
draft email + LinkedIn note per top contact, never sent by us; verify the
contacts we already have.
"""

import inspect
import json
from types import SimpleNamespace

import pytest

from app.services.enrichment import email_verification as ev
from app.services.outreach import deep_links, draft_outreach, rank_contacts


# --- verification --------------------------------------------------------

class TestHeuristic:
    def test_no_mx_is_invalid(self, monkeypatch):
        monkeypatch.setattr(ev, "_mx_exists", lambda d: False)
        v = ev.heuristic("jane@nowhere.example")
        assert v.status == "invalid" and v.method == "heuristic" and "MX" in v.detail

    def test_pattern_match_with_observed_address_is_likely_never_valid(self, monkeypatch):
        monkeypatch.setattr(ev, "_mx_exists", lambda d: True)
        v = ev.heuristic("jane.doe@acme.com", ["mark.smith@acme.com"])
        assert v.status == "likely"
        assert "observed" in v.detail
        v2 = ev.heuristic("jdoe@acme.com", ["mark.smith@acme.com"])
        assert v2.status == "unknown"  # a different shape than what we have seen

    def test_freemail_cannot_be_checked(self, monkeypatch):
        monkeypatch.setattr(ev, "_mx_exists", lambda d: True)
        assert ev.heuristic("someone@gmail.com").status == "unknown"

    def test_role_mailbox_is_likely(self, monkeypatch):
        monkeypatch.setattr(ev, "_mx_exists", lambda d: True)
        assert ev.heuristic("careers@acme.com").status == "likely"

    def test_malformed(self):
        assert ev.heuristic("not an email").status == "invalid"

    def test_dns_failure_is_unknown(self, monkeypatch):
        monkeypatch.setattr(ev, "_mx_exists", lambda d: None)
        assert ev.heuristic("a@acme.com").status == "unknown"


class TestVerifyEmailRouting:
    def _settings(self, monkeypatch, provider="", key=""):
        from pydantic import SecretStr

        from app import config

        s = SimpleNamespace(email_verify_provider=provider, email_verify_api_key=SecretStr(key))
        monkeypatch.setattr(config, "get_settings", lambda: s)

    def test_provider_wins_when_configured(self, monkeypatch):
        self._settings(monkeypatch, "hunter", "k")
        monkeypatch.setattr(ev, "_provider", lambda e, p, k: ev.Verification("valid", "hunter", "hunter: valid"))
        monkeypatch.setattr(ev, "port25_reachable", lambda: False)
        assert ev.verify_email("a@acme.com").method == "hunter"

    def test_provider_outage_falls_through_to_heuristic(self, monkeypatch):
        self._settings(monkeypatch, "zerobounce", "k")
        monkeypatch.setattr(ev, "_provider", lambda e, p, k: None)
        monkeypatch.setattr(ev, "port25_reachable", lambda: False)
        monkeypatch.setattr(ev, "_mx_exists", lambda d: True)
        assert ev.verify_email("careers@acme.com").method == "heuristic"

    def test_smtp_only_when_port_25_reachable(self, monkeypatch):
        self._settings(monkeypatch)
        monkeypatch.setattr(ev, "port25_reachable", lambda: False)
        monkeypatch.setattr(ev, "_mx_exists", lambda d: True)
        called = []
        import app.services.enrichment.email_verifier as smtp

        monkeypatch.setattr(smtp, "verify_email_smtp", lambda e: called.append(e) or {"status": "valid"})
        assert ev.verify_email("careers@acme.com").method == "heuristic"
        assert called == []
        monkeypatch.setattr(ev, "port25_reachable", lambda: True)
        assert ev.verify_email("careers@acme.com").method == "smtp"

    def test_hunter_mapping(self, monkeypatch):
        class R:
            status_code = 200

            def json(self):
                return {"data": {"status": "accept_all", "score": 60}}

        class C:
            def __init__(self, **k): ...
            def __enter__(self): return self
            def __exit__(self, *a): ...
            def get(self, *a, **k): return R()

        import httpx

        monkeypatch.setattr(httpx, "Client", C)
        assert ev._provider("a@b.co", "hunter", "k").status == "catch_all"

    def test_available_method(self, monkeypatch):
        self._settings(monkeypatch)
        monkeypatch.setattr(ev, "port25_reachable", lambda: False)
        assert ev.available_method() == "heuristic"
        self._settings(monkeypatch, "hunter", "k")
        assert ev.available_method() == "hunter"


def test_nightly_reverify_uses_the_new_layer_and_rechecks_likely():
    from app.workers.tasks import enrichment_task

    src = inspect.getsource(enrichment_task.verify_stale_emails)
    assert "email_verification" in src and '"likely"' in src and "verify_email_smtp" not in src


# --- ranking -------------------------------------------------------------

_LAST = ["Adams", "Baker", "Clark", "Davis", "Evans", "Foster", "Green", "Hill", "Irwin", "Jones", "Kane", "Lane"]


def _c(i, cat="hiring", email="x@acme.com", status="unverified", li="", conf=0.5, title="Talent Partner"):
    return SimpleNamespace(id=i, role_category=cat, email=email, email_status=status, linkedin_url=li,
                           confidence_score=conf, title=title, first_name="Alex", last_name=_LAST[i % len(_LAST)], seniority="other")


class TestRankContacts:
    def test_recruiters_first_then_engineering_then_execs_only_at_small_companies(self):
        big = SimpleNamespace(employee_count="5000")
        cs = [_c(1, "c_suite"), _c(2, "engineering_lead"), _c(3, "hiring"), _c(4, "other")]
        assert [c.id for c in rank_contacts(cs, {}, big)] == [3, 2, 1]
        small = SimpleNamespace(employee_count="40")
        assert [c.id for c in rank_contacts(cs, {}, small)] == [3, 2, 1]
        # at a big company the CEO ranks with "other"; a fourth hiring contact beats them
        cs.append(_c(5, "hiring"))
        assert 1 not in [c.id for c in rank_contacts(cs, {}, big)]

    def test_relevance_then_email_quality_break_ties(self):
        cs = [_c(1, "hiring", status="unverified"), _c(2, "hiring", status="valid"), _c(3, "hiring", status="likely")]
        assert [c.id for c in rank_contacts(cs, {}, None)] == [2, 3, 1]
        assert [c.id for c in rank_contacts(cs, {"1": 0.9}, None)][0] == 1

    def test_unreachable_contacts_are_dropped(self):
        cs = [_c(1, email="", li=""), _c(2, email="bad@acme.com", status="invalid"), _c(3, email="", li="https://linkedin.com/in/x")]
        assert [c.id for c in rank_contacts(cs, {}, None)] == [3]

    def test_limit(self):
        assert len(rank_contacts([_c(i) for i in range(10)], {}, None, limit=3)) == 3


# --- drafting ------------------------------------------------------------

class _Client:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        t = self.texts.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=t)], stop_reason="end_turn", stop_details=None)


ARGS = dict(job_title="Platform Engineer", company="Acme", company_facts="Industry: fintech", job_description="Kubernetes on AWS.",
            resume_text="Sarthak. 4 years SRE at Foo Corp running EKS.", book=[{"question": "LinkedIn", "answer": "https://linkedin.com/in/s"}],
            contact_name="Jane Doe", contact_title="Talent Partner")


def _j(**k):
    return json.dumps(k)


class TestDraftOutreach:
    def test_clean_draft_is_fact_checked_and_kept(self):
        c = _Client(_j(enough_information=True, email_subject="Platform Engineer application", email_body="Hi Jane, I applied for Platform Engineer. Four years on EKS at Foo Corp. 15 minutes?", linkedin_note="Hi Jane, applied for Platform Engineer at Acme, 4y EKS at Foo Corp. Happy to chat."),
                    _j(unsupported_claims=[]))
        d = draft_outreach(**ARGS, client=c)
        assert d.email_body.startswith("Hi Jane") and d.unsupported_claims == [] and "traced back" in d.note
        assert len(c.calls) == 2
        assert "Jane Doe, Talent Partner" in c.calls[0]["messages"][0]["content"]
        assert len(d.linkedin_note) <= 200

    def test_unsupported_claim_triggers_one_revision_and_recheck(self):
        c = _Client(_j(enough_information=True, email_subject="s", email_body="I led a 40-person team at Google.", linkedin_note="n"),
                    _j(unsupported_claims=["led a 40-person team at Google"]),
                    _j(email_subject="s", email_body="I ran EKS at Foo Corp for four years.", linkedin_note="n"),
                    _j(unsupported_claims=[]))
        d = draft_outreach(**ARGS, client=c)
        assert "Google" not in d.email_body and d.unsupported_claims == [] and "Revised" in d.note
        assert len(c.calls) == 4

    def test_still_flagged_after_revision_is_shown_to_the_user(self):
        c = _Client(_j(enough_information=True, email_subject="s", email_body="Google.", linkedin_note="n"),
                    _j(unsupported_claims=["Google"]),
                    _j(email_subject="s", email_body="Still Google.", linkedin_note="n"),
                    _j(unsupported_claims=["Google"]))
        d = draft_outreach(**ARGS, client=c)
        assert d.unsupported_claims == ["Google"] and "still flags" in d.note

    def test_nothing_concrete_means_no_draft(self):
        d = draft_outreach(**ARGS, client=_Client(_j(enough_information=False, email_subject="", email_body="", linkedin_note="")))
        assert not d.email_body and d.enough_information is False and "nothing was drafted" in d.note

    def test_em_dashes_removed_and_note_capped(self):
        c = _Client(_j(enough_information=True, email_subject="s", email_body="EKS — four years.", linkedin_note="x" * 300), _j(unsupported_claims=[]))
        d = draft_outreach(**ARGS, client=c)
        assert "—" not in d.email_body and len(d.linkedin_note) == 200

    def test_model_failure_never_raises(self):
        class Boom:
            messages = SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("down")))

        assert draft_outreach(**ARGS, client=Boom()).error == "draft failed"

    def test_system_prompt_forbids_invention_and_ai_mentions(self):
        from app.services import outreach

        s = outreach._DRAFT_SYSTEM
        assert "Never invent" in s and "200 characters" in s and "no mention of AI" in s


def test_deep_links_encode_and_skip_missing_address():
    assert deep_links("", "s", "b") == {}
    links = deep_links("jane@acme.com", "Hi & bye", "line one\nline two")
    assert links["gmail"].startswith("https://mail.google.com/mail/?view=cm&fs=1&to=jane%40acme.com&su=Hi%20%26%20bye&body=line%20one%0Aline%20two")
    assert "outlook.office.com" in links["outlook"] and links["mailto"].startswith("mailto:jane@acme.com?")


# --- task + endpoints ----------------------------------------------------

def test_outreach_tasks_are_registered_with_limits():
    from app.workers import tasks
    from app.workers.tasks.outreach_task import draft_outreach_task, verify_company_contacts_task

    assert "draft_outreach_task" in tasks.__all__ and "verify_company_contacts_task" in tasks.__all__
    for t in (draft_outreach_task, verify_company_contacts_task):
        assert t.acks_late is False and t.time_limit and t.soft_time_limit < t.time_limit


def test_task_keeps_user_edited_drafts_and_verifies_before_drafting():
    from app.workers.tasks import outreach_task

    src = inspect.getsource(outreach_task._run)
    assert "keep a draft the user may have edited" in src
    assert src.index("verify_email(") < src.index("draft_outreach(")
    assert "rank_contacts(contacts, relevance, company, limit=3)" in src


def test_endpoints_exist_and_never_send():
    from app.api.v1 import applications, companies

    for name in ("get_outreach", "draft_outreach", "outreach_sent", "outreach_edit"):
        assert hasattr(applications, name)
    src = inspect.getsource(applications.outreach_sent)
    assert "smtplib" not in src and "outreach_status" in src and "last_outreach_at" in src
    assert "retry=False" in inspect.getsource(applications.draft_outreach)
    assert hasattr(companies, "verify_company_contacts") and "messaged" in companies._VALID_OUTREACH


def test_settings_have_provider_fields():
    from app.config import Settings

    f = Settings.model_fields
    assert "email_verify_provider" in f and "email_verify_api_key" in f


class TestPlausiblePerson:
    """Prod contacts for Camunda / Cloudflare / Supabase / Northflank were
    mostly scraped junk: nav items glued into names, investors quoted on
    the site with guessed emails, "for technical" as a recruiter."""

    def _p(self, first, last, title, email="", company="Supabase"):
        from app.services.outreach import plausible_person

        return plausible_person(SimpleNamespace(first_name=first, last_name=last, title=title, email=email), SimpleNamespace(name=company))

    def test_junk_from_prod_is_rejected(self):
        assert not self._p("SolutionsClose", "SolutionsOpen Solutions", "Public Sector", "solutionsclose.solutionsopensolutions@camunda.com", "Camunda")
        assert not self._p("Adopt", "AI", "IndustriesHealthcareFinancial servicesRetailGamingPublic sector", company="Cloudflare")
        assert not self._p("AI", "Gateway", "Developers Discord", company="Cloudflare")
        assert not self._p("Test", "Drive", "Reference architectureTechnical guides", company="Cloudflare")
        assert not self._p("for", "technical", "Recruiter / Hiring Contact", company="Northflank")
        assert not self._p("Jason", "WarnerGitHub", "GitHub CTO")

    def test_other_companies_people_quoted_on_the_site_are_rejected(self):
        assert not self._p("Tom", "Preston-Werner", "GitHub Cofounder", "tom@supabase.com")
        assert not self._p("Harold", "Giménez", "HashiCorp - VP Eng")
        assert not self._p("Pedro", "Canahuati", "1Password CTO, Ex-Facebook")
        assert not self._p("Guillermo", "Rauch", "Vercel Founder")

    def test_real_staff_pass(self):
        assert self._p("Barbie", "Brewer", "CPO", company="Camunda")
        assert self._p("Bernd", "Ruecker", "Chief Technologist and Co-Founder", company="Camunda")
        assert self._p("Ant", "Wilson", "CTO and Co-Founder")
        assert self._p("Jane", "Doe", "Senior Engineering Manager, Platform")
        assert self._p("Jane", "Doe", "Supabase Talent Partner")

    def test_role_mailboxes_allowed_unnamed_persons_not(self):
        assert self._p("", "", "HR Contact", "hr@supabase.com")
        assert not self._p("", "", "CEO", "ceo@camunda.com", "Camunda")

    def test_rank_applies_the_filter(self):
        junk = _c(1, "hiring")
        junk.first_name, junk.last_name, junk.title = "for", "technical", "Recruiter"
        assert rank_contacts([junk, _c(2, "hiring")], {}, SimpleNamespace(name="X", employee_count=""))[0].id == 2
