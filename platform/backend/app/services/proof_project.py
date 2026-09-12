"""F406 — a small project that shows the candidate's skills to the people
who would hire them ("show, don't tell").

Two stages, and only the first exists so far, on purpose: Sarthak asked
for the idea system to be built and tested across our job and company
data BEFORE any project code is generated.

Stage 1 (this module):
  * ``gather_research`` — everything we can read about the role and the
    company without logging in anywhere: the job description, the
    company profile, the company's other open roles (what they are
    building), the public GitHub organisation (top repositories,
    languages), the status page (recent incidents = real pain), and the
    engineering blog / changelog titles (what they talk about). Every
    source gets an id so an idea can cite it.
  * ``generate_ideas`` — Opus proposes three project ideas tied to that
    research, each with verbatim evidence quotes, what to build, the
    skills it shows and who to send it to.
  * ``ground_ideas`` — a programmatic check that every quote really
    appears in the research. An idea whose evidence does not trace is
    marked ungrounded and demoted; nothing is shown as "company-specific"
    on the model's say-so.
  * ``corpus_term_coverage`` + ``generate_generic_ideas`` — when the
    research gives nothing specific, ideas that fit most of the openings
    we track: the share of job descriptions each idea's skills appear in
    is computed from our own data, not guessed.

Stage 2 (not built): generating the project code for a chosen idea.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill taxonomy for the corpus coverage numbers (infra / SRE / security).
# id → (label, regex). Regexes are word-bounded and case-insensitive.
# ---------------------------------------------------------------------------
SKILL_TERMS: dict[str, tuple[str, str]] = {
    "kubernetes": ("Kubernetes", r"\bkubernetes\b|\bk8s\b|\beks\b|\bgke\b|\baks\b"),
    "docker": ("Docker / containers", r"\bdocker\b|\bcontainers?\b|\bcontainerd\b"),
    "terraform": ("Terraform / IaC", r"\bterraform\b|\bopentofu\b|\binfrastructure as code\b|\biac\b|\bpulumi\b|\bcloudformation\b"),
    "helm": ("Helm", r"\bhelm\b"),
    "argocd": ("GitOps (ArgoCD / Flux)", r"\bargo ?cd\b|\bflux ?cd\b|\bgitops\b"),
    "cicd": ("CI/CD pipelines", r"\bci/?cd\b|\bcontinuous (integration|delivery|deployment)\b|\bgithub actions\b|\bgitlab ci\b|\bjenkins\b|\bcircleci\b|\bbuildkite\b"),
    "aws": ("AWS", r"\baws\b|\bamazon web services\b"),
    "gcp": ("GCP", r"\bgcp\b|\bgoogle cloud\b"),
    "azure": ("Azure", r"\bazure\b"),
    "observability": ("Observability (Prometheus / Grafana / OTel)", r"\bprometheus\b|\bgrafana\b|\bopentelemetry\b|\botel\b|\bobservability\b|\bdatadog\b|\bnew relic\b"),
    "logging": ("Logging (ELK / Loki)", r"\belk\b|\belasticsearch\b|\bloki\b|\bfluent ?bit\b|\bfluentd\b|\bsplunk\b"),
    "sre": ("SRE practice (SLOs, on-call, incident response)", r"\bslos?\b|\bslis?\b|\berror budgets?\b|\bon-?call\b|\bincident (response|management)\b|\bpostmortems?\b"),
    "linux": ("Linux", r"\blinux\b|\bubuntu\b|\bdebian\b|\brhel\b"),
    "python": ("Python", r"\bpython\b"),
    "go": ("Go", r"\bgolang\b|\bgo\b(?= (?:lang|programming|services?|microservices?|,|and|or|/))"),
    "bash": ("Bash / shell scripting", r"\bbash\b|\bshell script"),
    "networking": ("Networking (VPC, DNS, load balancing)", r"\bvpc\b|\bdns\b|\bload balanc|\bnginx\b|\benvoy\b|\bistio\b|\bservice mesh\b|\bnetworking\b"),
    "security": ("Security hardening", r"\bsecurity\b|\bhardening\b|\bcis benchmark|\bvulnerabilit"),
    "iam": ("IAM / secrets management", r"\biam\b|\bvault\b|\bsecrets? manag|\bsops\b|\bkms\b"),
    "compliance": ("Compliance (SOC 2 / ISO 27001)", r"\bsoc ?2\b|\biso ?27001\b|\bhipaa\b|\bpci\b|\bgdpr\b|\bcompliance\b"),
    "supply_chain": ("Supply-chain security (SBOM, signing, scanning)", r"\bsbom\b|\bsigstore\b|\bcosign\b|\btrivy\b|\bsnyk\b|\bimage scanning\b|\bsupply.chain\b"),
    "postgres": ("PostgreSQL / databases", r"\bpostgres(ql)?\b|\bmysql\b|\brds\b|\bdatabase administration\b"),
    "redis": ("Redis / caching", r"\bredis\b|\bmemcached\b"),
    "kafka": ("Kafka / streaming", r"\bkafka\b|\bpub/?sub\b|\bkinesis\b|\bredpanda\b|\bnats\b"),
    "cost": ("Cloud cost optimisation (FinOps)", r"\bcost optimi[sz]ation\b|\bfinops\b|\bcloud costs?\b|\bcost[- ]efficien"),
    "autoscaling": ("Autoscaling / capacity", r"\bautoscal|\bcapacity planning\b|\bkeda\b|\bkarpenter\b"),
    "ml_infra": ("ML / GPU infrastructure", r"\bgpu\b|\bmlops\b|\bml platform\b|\binference\b|\bkuberay\b|\bray\b|\bcuda\b"),
    "serverless": ("Serverless", r"\bserverless\b|\blambda\b|\bcloud functions\b|\bcloud run\b"),
    "ansible": ("Ansible / config management", r"\bansible\b|\bpuppet\b|\bchef\b|\bsalt(stack)?\b"),
    "multi_cloud": ("Multi-cloud / hybrid", r"\bmulti-?cloud\b|\bhybrid cloud\b|\bon-?prem"),
    "dr": ("Backup / disaster recovery", r"\bdisaster recovery\b|\bbackups?\b|\bbusiness continuity\b|\brto\b|\brpo\b"),
    "platform": ("Internal developer platform / golden paths", r"\bdeveloper (platform|experience|portal)\b|\bbackstage\b|\bgolden path|\bplatform team\b|\bidp\b"),
    "migration": ("Cloud / Kubernetes migration", r"\bmigrat"),
    "performance": ("Performance / load testing", r"\bload test|\bk6\b|\blocust\b|\bperformance (testing|tuning|engineering)\b|\blatency\b"),
    "edge": ("Edge / CDN", r"\bcdn\b|\bcloudflare\b|\bedge computing\b|\bfastly\b"),
    "zero_trust": ("Zero trust / identity", r"\bzero.trust\b|\bsso\b|\boidc\b|\boauth\b|\bsaml\b"),
}

_COMPILED = {k: re.compile(v[1], re.I) for k, v in SKILL_TERMS.items()}


def terms_in(text: str) -> set[str]:
    t = text or ""
    return {k for k, rx in _COMPILED.items() if rx.search(t)}


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------
_UA = {"User-Agent": "Mozilla/5.0 (compatible; sales-platform-research/1.0)"}


def _domain(company) -> str:
    d = (getattr(company, "domain", "") or "").strip().lower()
    if not d:
        w = (getattr(company, "website", "") or "").strip()
        if w:
            d = (urlparse(w if "://" in w else f"https://{w}").hostname or "").lower()
    return d[4:] if d.startswith("www.") else d


def _slugs(company) -> list[str]:
    out = []
    d = _domain(company)
    if d:
        out.append(d.split(".")[0])
    name = re.sub(r"[^a-z0-9]+", "", (getattr(company, "name", "") or "").lower())
    if name and name not in out:
        out.append(name)
    name2 = re.sub(r"[^a-z0-9]+", "-", (getattr(company, "name", "") or "").lower()).strip("-")
    if name2 and name2 not in out:
        out.append(name2)
    return [s for s in out if len(s) >= 3][:3]


def _github_org(client, company) -> dict | None:
    """Public organisation + top repositories. Unauthenticated: 60 req/h,
    and we spend at most 2 per company."""
    d = _domain(company)
    for slug in _slugs(company):
        try:
            r = client.get(f"https://api.github.com/orgs/{slug}", headers={**_UA, "Accept": "application/vnd.github+json"})
        except Exception:
            continue
        if r.status_code == 403:
            return {"error": "github rate limit"}
        if r.status_code != 200:
            continue
        org = r.json()
        blog = (org.get("blog") or "").lower()
        # Accept only when the org points back at the company (its blog /
        # website mentions the domain) or the slug is the domain label.
        if d and d not in blog and slug != d.split(".")[0]:
            continue
        try:
            rr = client.get(f"https://api.github.com/orgs/{slug}/repos", params={"sort": "pushed", "per_page": 40, "type": "public"},
                            headers={**_UA, "Accept": "application/vnd.github+json"})
            repos = rr.json() if rr.status_code == 200 else []
        except Exception:
            repos = []
        repos = sorted([x for x in repos if isinstance(x, dict) and not x.get("archived") and not x.get("fork")],
                       key=lambda x: (-(x.get("stargazers_count") or 0), x.get("pushed_at") or ""))[:10]
        return {"login": org.get("login"), "url": org.get("html_url"), "public_repos": org.get("public_repos"),
                "repos": [{"name": x.get("name"), "description": (x.get("description") or "")[:200], "language": x.get("language"),
                           "stars": x.get("stargazers_count"), "pushed_at": (x.get("pushed_at") or "")[:10],
                           "topics": (x.get("topics") or [])[:8]} for x in repos]}
    return None


def _status_page(client, company) -> list[dict]:
    d = _domain(company)
    if not d:
        return []
    for host in (f"status.{d}", f"{d.split('.')[0]}.statuspage.io"):
        try:
            r = client.get(f"https://{host}/api/v2/incidents.json", headers=_UA)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue
        out = []
        for inc in (data.get("incidents") or [])[:8]:
            out.append({"name": (inc.get("name") or "")[:160], "impact": inc.get("impact"), "created_at": (inc.get("created_at") or "")[:10],
                        "status": inc.get("status"), "url": inc.get("shortlink") or f"https://{host}"})
        return out
    return []


_TITLE_MIN, _TITLE_MAX = 25, 120


def _blog_titles(client, company) -> dict:
    d = _domain(company)
    if not d:
        return {}
    for url in (f"https://{d}/blog", f"https://blog.{d}", f"https://{d}/engineering", f"https://engineering.{d}", f"https://{d}/changelog"):
        try:
            r = client.get(url, headers=_UA)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            titles, seen = [], set()
            for el in soup.find_all(["h1", "h2", "h3", "a"]):
                t = re.sub(r"\s+", " ", el.get_text(" ")).strip()
                if _TITLE_MIN <= len(t) <= _TITLE_MAX and t.lower() not in seen and not re.search(r"cookie|privacy|sign in|log in|subscribe", t, re.I):
                    seen.add(t.lower())
                    titles.append(t)
                if len(titles) >= 15:
                    break
            if len(titles) >= 3:
                return {"url": str(r.url), "titles": titles}
        except Exception:
            continue
    return {}


def gather_research(session, job, company, *, fetch_external: bool = True, job_description: str = "") -> dict:
    """Everything an idea may cite. ``sources`` is a list of
    {id, kind, title, url, text}; ``text`` is what quotes are checked
    against."""
    from sqlalchemy import select

    from app.models.job import Job

    sources: list[dict] = []
    jd = (job_description or getattr(getattr(job, "description", None), "text_content", "") or "").strip()
    sources.append({"id": "JD", "kind": "job_description", "title": f"Job description: {job.title}", "url": job.url or "",
                    "text": jd[:8000] if jd else "(no description available for this posting)"})

    bits = []
    for label, attr in (("Industry", "industry"), ("Size", "employee_count"), ("Headquarters", "headquarters"),
                        ("Funding stage", "funding_stage"), ("Total funding", "total_funding"), ("Website", "website")):
        v = getattr(company, attr, "") or ""
        if v:
            bits.append(f"{label}: {v}")
    ts = getattr(company, "tech_stack", None) or []
    if ts:
        bits.append("Tech stack (from enrichment): " + ", ".join(str(x) for x in ts[:25]))
    desc = (getattr(company, "description", "") or "").strip()
    if desc:
        bits.append(f"About: {desc[:1500]}")
    sources.append({"id": "PROFILE", "kind": "company_profile", "title": f"Company profile: {getattr(company, 'name', '')}",
                    "url": getattr(company, "website", "") or "", "text": "\n".join(bits) or "(nothing on file)"})

    others = session.execute(select(Job).where(Job.company_id == job.company_id, Job.id != job.id,
                                               Job.status.in_(["new", "under_review", "accepted"])).limit(40)).scalars().all()
    if others:
        lines = []
        for o in others:
            snippet = (getattr(getattr(o, "description", None), "text_content", "") or "")[:300].replace("\n", " ")
            lines.append(f"- {o.title}" + (f" ({o.location_raw})" if getattr(o, "location_raw", "") else "") + (f": {snippet}" if snippet else ""))
        sources.append({"id": "JOBS", "kind": "other_openings", "title": f"{len(others)} other open roles at {getattr(company, 'name', '')}",
                        "url": "", "text": "\n".join(lines)[:6000]})

    found = {"github": False, "status": False, "blog": False, "errors": []}
    if fetch_external:
        import httpx

        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            gh = _github_org(client, company)
            if gh and gh.get("error"):
                found["errors"].append(gh["error"])
            elif gh and gh.get("repos"):
                found["github"] = True
                lines = [f"- {r['name']} ({r.get('language') or '?'}, {r.get('stars') or 0} stars, last push {r.get('pushed_at')}): {r.get('description') or ''}"
                         + (f" [topics: {', '.join(r['topics'])}]" if r.get("topics") else "") for r in gh["repos"]]
                sources.append({"id": "GITHUB", "kind": "github", "title": f"GitHub organisation {gh['login']} ({gh.get('public_repos')} public repos)",
                                "url": gh.get("url") or "", "text": "\n".join(lines)})
            inc = _status_page(client, company)
            if inc:
                found["status"] = True
                sources.append({"id": "STATUS", "kind": "status_page", "title": f"Status page: {len(inc)} recent incidents", "url": inc[0].get("url") or "",
                                "text": "\n".join(f"- {i['created_at']} [{i.get('impact')}] {i['name']}" for i in inc)})
            blog = _blog_titles(client, company)
            if blog:
                found["blog"] = True
                sources.append({"id": "BLOG", "kind": "blog", "title": "Engineering blog / changelog titles", "url": blog["url"],
                                "text": "\n".join(f"- {t}" for t in blog["titles"])})

    return {"gathered_at": datetime.now(timezone.utc).isoformat(), "job_title": job.title, "company": getattr(company, "name", "") or "",
            "sources": sources, "found": found, "jd_terms": sorted(terms_in(jd))}


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------
_IDEAS_SYSTEM = """You propose small, concrete engineering projects a job candidate can build in one or two days and send to the people hiring for a role, to show skill instead of claiming it.

You are given RESEARCH about the company and role (each source has an id) and the candidate's RÉSUMÉ. Propose exactly 3 ideas, ordered best first.

Hard rules:
- Every idea must rest on the research. Cite it: each "evidence" item is {"source": "<source id>", "quote": "<verbatim text copied from that source, 5-30 words>"}. Copy quotes exactly; do not paraphrase inside "quote". An idea with no real evidence must be honest: set "angle" to "generic" and cite the JD only for the skills it asks for.
- Prefer, in this order: (1) a real pain the company shows (status-page incidents, a repo issue pattern, a blog post about a struggle, a cluster of similar openings) that the candidate's résumé skills can address; (2) something the company is building or migrating to (blog, other openings, GitHub) where a working example of the candidate's approach would be useful; (3) a match to the stack in the job description.
- Use only skills the résumé actually shows. Never assume access to the company's private systems, data or accounts; the project uses public information, the candidate's own cloud/lab, or synthetic data.
- Scope: buildable in 4-16 hours; a public repository with a README, plus a short write-up the recipient can read in two minutes. Say exactly what would be built and what the recipient would see.
- "recipient_role": who at the company should receive it (e.g. "DevOps lead", "Head of Platform", "Engineering manager for the team in the posting", "Product engineer") and why them.
- No marketing language, no em dashes.

Respond with JSON only:
{"ideas": [{"title": "...", "angle": "pain|initiative|stack_match|generic", "summary": "2-3 sentences: the problem or topic and what the project does about it",
  "evidence": [{"source": "JD", "quote": "..."}], "build": ["step", "step", "step"], "deliverable": "what the recipient opens and sees",
  "skills_shown": ["..."], "recipient_role": "...", "why_them": "...", "effort_hours": 8, "risks": "one line: what could make this land badly"}],
 "research_verdict": "one or two sentences: was there anything company-specific worth building on, and what was the strongest signal"}"""

_GENERIC_SYSTEM = """You design a small portfolio of 6 project ideas for a DevOps / SRE / platform / security engineer, each buildable in one or two days, each meant to be sent to hiring managers at MANY companies with a short note. You are given the skills that appear most often across the job descriptions we track (with the share of postings mentioning each, by term id) and the candidate's RÉSUMÉ.

Rules:
- Each idea names the term ids it demonstrates ("terms": 2-5 ids from the list). Choose so the six ideas together cover the highest-share terms; do not make six variations of the same thing.
- Use only skills the résumé shows. Concrete: what is built, what the reader sees in two minutes, why a hiring manager for that skill cares.
- Buildable in 4-16 hours, public repo + README + 2-minute write-up. Synthetic data or the candidate's own lab only.
- No marketing language, no em dashes.

Respond with JSON only:
{"ideas": [{"title": "...", "terms": ["kubernetes", "observability"], "summary": "...", "build": ["...", "..."], "deliverable": "...", "skills_shown": ["..."], "effort_hours": 8, "recipient_role": "..."}]}"""


def _json_block(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end])


def _research_text(research: dict) -> str:
    parts = []
    for s in research.get("sources", []):
        parts.append(f"### SOURCE {s['id']} — {s['title']}" + (f" ({s['url']})" if s.get("url") else "") + f"\n{s['text']}")
    return "\n\n".join(parts)


def generate_ideas(research: dict, resume_text: str, client=None) -> dict:
    from app.ai_client import AIRefused, AIUnavailable, complete

    prompt = (f"RESEARCH ON {research.get('company')} — ROLE: {research.get('job_title')}\n\n{_research_text(research)}\n\n"
              f"CANDIDATE RÉSUMÉ:\n{(resume_text or '')[:9000]}")
    try:
        text, _ = complete(prompt, system=_IDEAS_SYSTEM, answer_tokens=2500, client=client)
        data = _json_block(text)
    except AIUnavailable:
        return {"ideas": [], "error": "AI is not configured"}
    except AIRefused as exc:
        return {"ideas": [], "error": f"the model declined: {exc.category or 'unspecified'}"}
    except Exception as exc:
        logger.info("proof_project: idea generation failed: %s", exc)
        return {"ideas": [], "error": "idea generation failed"}
    ideas = [i for i in (data.get("ideas") or []) if isinstance(i, dict) and i.get("title")][:3]
    for n, i in enumerate(ideas):
        i["id"] = f"idea-{n + 1}"
        i["summary"] = re.sub(r"\s*—\s*", ", ", str(i.get("summary") or ""))
    return {"ideas": ideas, "research_verdict": re.sub(r"\s*—\s*", ", ", str(data.get("research_verdict") or "")), "error": ""}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def quote_grounded(quote: str, source_text: str) -> bool:
    q, t = _norm(quote), _norm(source_text)
    if not q or not t:
        return False
    qt = [w for w in q.split() if len(w) > 2]
    if len(qt) < 3:
        return False  # "CI" matches everything and proves nothing
    if q in t:
        return True
    tw = set(t.split())
    return sum(1 for w in qt if w in tw) / len(qt) >= 0.8


def ground_ideas(result: dict, research: dict) -> dict:
    """Check every quote against its source; classify each idea."""
    by_id = {s["id"]: s for s in research.get("sources", [])}
    graded = []
    for idea in result.get("ideas") or []:
        ev_out, company_specific, role_specific = [], False, False
        for ev in idea.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            src = by_id.get(str(ev.get("source") or "").upper())
            ok = bool(src) and quote_grounded(str(ev.get("quote") or ""), src["text"])
            ev_out.append({"source": ev.get("source"), "quote": str(ev.get("quote") or "")[:300], "grounded": ok,
                           "source_title": src["title"] if src else "(unknown source)", "url": (src or {}).get("url", "")})
            if ok and src["id"] in ("GITHUB", "STATUS", "BLOG", "JOBS", "PROFILE"):
                company_specific = True
            if ok and src["id"] == "JD":
                role_specific = True
        specificity = "company" if company_specific else "role" if role_specific else "ungrounded"
        graded.append({**idea, "evidence": ev_out, "specificity": specificity,
                       "grounded_evidence": sum(1 for e in ev_out if e["grounded"]), "total_evidence": len(ev_out)})
    order = {"company": 0, "role": 1, "ungrounded": 2}
    graded.sort(key=lambda i: (order[i["specificity"]], -i["grounded_evidence"]))
    return {**result, "ideas": graded}


# ---------------------------------------------------------------------------
# Generic ideas from the corpus
# ---------------------------------------------------------------------------
def corpus_term_coverage(session, limit: int = 600) -> dict:
    """Share of tracked, relevant job descriptions mentioning each term."""
    from sqlalchemy import select

    from app.models.job import Job, JobDescription

    rows = session.execute(
        select(Job.id, Job.role_cluster, JobDescription.text_content)
        .join(JobDescription, JobDescription.job_id == Job.id)
        .where(Job.status.in_(["new", "under_review", "accepted"]), Job.role_cluster.in_(["infra", "security"]))
        .order_by(Job.first_seen_at.desc()).limit(limit)
    ).all()
    per_job = [terms_in(text or "") for _, _, text in rows]
    n = len(per_job) or 1
    counts: dict[str, int] = {k: 0 for k in SKILL_TERMS}
    for ts in per_job:
        for t in ts:
            counts[t] += 1
    coverage = {k: round(100.0 * v / n, 1) for k, v in counts.items()}
    return {"jobs": len(per_job), "coverage": coverage, "per_job": [sorted(t) for t in per_job],
            "computed_at": datetime.now(timezone.utc).isoformat()}


def idea_coverage(terms: list[str], per_job: list[list[str]]) -> float:
    """Share of JDs mentioning at least two of the idea's terms (one, when
    the idea has a single term)."""
    if not per_job or not terms:
        return 0.0
    need = 1 if len(terms) == 1 else 2
    ts = set(terms)
    hits = sum(1 for job in per_job if len(ts & set(job)) >= need)
    return round(100.0 * hits / len(per_job), 1)


def generate_generic_ideas(corpus: dict, resume_text: str, client=None) -> dict:
    from app.ai_client import AIRefused, AIUnavailable, complete

    top = sorted(corpus["coverage"].items(), key=lambda kv: -kv[1])[:24]
    listing = "\n".join(f"- {k}: {SKILL_TERMS[k][0]} — in {v}% of {corpus['jobs']} postings" for k, v in top)
    prompt = f"MOST-ASKED SKILLS (term id: label — share of postings):\n{listing}\n\nCANDIDATE RÉSUMÉ:\n{(resume_text or '')[:9000]}"
    try:
        text, _ = complete(prompt, system=_GENERIC_SYSTEM, answer_tokens=2500, client=client)
        data = _json_block(text)
    except AIUnavailable:
        return {"ideas": [], "error": "AI is not configured"}
    except AIRefused as exc:
        return {"ideas": [], "error": f"the model declined: {exc.category or 'unspecified'}"}
    except Exception as exc:
        logger.info("proof_project: generic ideas failed: %s", exc)
        return {"ideas": [], "error": "idea generation failed"}
    ideas = []
    for n, i in enumerate((data.get("ideas") or [])[:6]):
        if not isinstance(i, dict) or not i.get("title"):
            continue
        terms = [t for t in (i.get("terms") or []) if t in SKILL_TERMS]
        ideas.append({**i, "id": f"generic-{n + 1}", "terms": terms, "term_labels": [SKILL_TERMS[t][0] for t in terms],
                      "coverage_pct": idea_coverage(terms, corpus.get("per_job") or []),
                      "summary": re.sub(r"\s*—\s*", ", ", str(i.get("summary") or ""))})
    ideas.sort(key=lambda i: -i["coverage_pct"])
    return {"ideas": ideas, "jobs_in_corpus": corpus["jobs"], "computed_at": corpus.get("computed_at"), "error": ""}
