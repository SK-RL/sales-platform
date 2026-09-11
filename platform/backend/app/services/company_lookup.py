"""Find an aggregator repost's real posting by company + title.

F375. When the aggregator page itself is walled (Himalayas serves the
prod VM Cloudflare's challenge on every page — F374), the repost still
tells us two things the ATSes' public APIs can answer directly: the
company and the title. Three lookups, cheapest first:

1. catalogue — a posting from the same company, same title, already
   scanned on an ATS we drive;
2. slug probe — board slugs derived from the company name, tried
   against the public Greenhouse / Ashby / Lever / Workable / Recruitee
   APIs (a missing board is an empty list, verified live), then the
   title matched on the board;
3. the company's careers page, fingerprinted with ``ats_fingerprint``,
   when we know its domain.

A match becomes a catalogue Job through the own-link path (F371), so
prepare/readiness/sweep use the employer's form. No match is recorded
as ``unmatched`` — measured, not guessed.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

logger = logging.getLogger(__name__)

_SUFFIXES = (
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "gmbh", "ag", "sa", "s.a.", "plc",
    "corp", "corp.", "corporation", "co", "co.", "company", "holdings", "group", "technologies",
    "technology", "labs", "the",
)
_TITLE_NOISE = re.compile(
    r"\((?:remote|hybrid|[^)]*remote[^)]*|[^)]*m/f/d[^)]*|[^)]*f/m/d[^)]*)\)|\b(?:100 ?%|fully) ?remote\b|"
    r"\bremote\b|\b(?:m/f/d|f/m/d|w/m/d|m/w/d)\b|[–—-]\s*(?:us|usa|uk|eu|emea|apac|canada|india|germany)\b",
    re.I,
)

# The drivable set, plus Lever: a Lever match is still the real form
# (extractable, walled) and better than a repost.
PROBE_PLATFORMS: tuple[str, ...] = ("greenhouse", "ashby", "lever", "workable", "recruitee", "breezy", "personio", "rippling")


def normalise_company(name: str) -> str:
    n = re.sub(r"[^\w\s&]", " ", (name or "").lower())
    words = [w for w in n.split() if w not in _SUFFIXES]
    return " ".join(words)


def slug_candidates(name: str) -> list[str]:
    """Board slugs a company is likely to have used, most likely first."""
    base = normalise_company(name).replace("&", " and ")
    words = base.split()
    if not words:
        return []
    out: list[str] = []
    # Deliberately NOT the lone first word: "general" (General Dynamics)
    # or "public" (Public Partnerships) would be someone else's board, and
    # a title match on the wrong board applies to the wrong company.
    for s in ("".join(words), "-".join(words)):
        if s and len(s) >= 3 and s not in out:
            out.append(s)
    raw = re.sub(r"[^a-z0-9-]", "", (name or "").lower().replace(" ", "-"))
    if raw and raw not in out and len(raw) >= 3:
        out.append(raw)
    return out[:4]


def normalise_title(title: str) -> str:
    t = _TITLE_NOISE.sub(" ", title or "")
    t = re.sub(r"[^\w\s]", " ", t.lower())
    t = re.sub(r"\b(?:sr|senior|jr|junior|staff|principal|lead)\b", lambda m: m.group(0), t)
    return " ".join(t.split())


def titles_match(a: str, b: str) -> bool:
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # one is the other plus a trailing qualifier ("Site Reliability
    # Engineer" vs "Site Reliability Engineer, Platform") — a prefix, at
    # least three words. Not containment: "Senior Site Reliability
    # Engineer - Vulnerability" is a different role, and the own-link
    # path would then apply to it.
    short, long_ = sorted((na, nb), key=len)
    return len(short.split()) >= 3 and long_.startswith(short + " ")


def find_in_catalogue(session, company_name: str, title: str):
    """A drivable posting for this company+title already in the catalogue."""
    from app.models.company import Company
    from app.models.job import Job
    from app.services.submitters import auto_submittable_platforms

    key = normalise_company(company_name)
    if not key:
        return None
    first = key.split()[0]
    companies = session.execute(
        select(Company).where(Company.name.ilike(f"%{first}%"))
    ).scalars().all()
    ids = [c.id for c in companies if normalise_company(c.name) == key]
    if not ids:
        return None
    rows = session.execute(
        select(Job).where(
            Job.company_id.in_(ids),
            Job.platform.in_(list(auto_submittable_platforms())),
            Job.status.notin_(["expired", "archived"]),
        ).order_by(Job.first_seen_at.desc()).limit(200)
    ).scalars().all()
    return next((j for j in rows if titles_match(j.title, title)), None)


def probe_boards(company_name: str, title: str, fetch=None) -> tuple[str, str, dict] | None:
    """(platform, slug, raw_job) for the first board that lists the title."""
    from app.fetchers import FETCHER_MAP

    fetch = fetch or (lambda p, s: FETCHER_MAP[p]().fetch(s) or [])
    for slug in slug_candidates(company_name):
        for platform in PROBE_PLATFORMS:
            try:
                jobs = fetch(platform, slug)
            except Exception:
                logger.info("company_lookup: probe %s/%s failed", platform, slug, exc_info=True)
                continue
            if not jobs:
                continue
            hit = next((r for r in jobs if titles_match(r.get("title", ""), title)), None)
            if hit:
                return platform, slug, hit
            # The board exists but this title isn't on it: other slugs
            # for the same company are unlikely to be a different board.
            return None
    return None


def fingerprint_company(session, company_id) -> list[tuple[str, str]]:
    """(platform, slug) boards from the company's careers page, if we know a domain."""
    from app.models.company import Company
    from app.services.ats_fingerprint import detect_ats_from_url

    c = session.get(Company, company_id) if company_id else None
    site = (getattr(c, "website", "") or getattr(c, "domain", "") or "").strip() if c else ""
    if not site:
        return []
    if "://" not in site:
        site = "https://" + site
    try:
        return [(fp.platform, fp.slug) for fp in detect_ats_from_url(site) if fp.platform in PROBE_PLATFORMS]
    except Exception:
        logger.info("company_lookup: fingerprint failed for %s", site, exc_info=True)
        return []


def canonical_posting_url(platform: str, slug: str, raw: dict) -> str:
    """The hosted-board URL for a probe hit.

    A board's API can hand back the company's *embedded* page as the
    posting URL — Bishop Fox's Greenhouse board returns
    ``bishopfox.com/jobs?gh_jid=7905132`` — which own-link rightly
    refuses (no board slug in it). We already know the slug and the id
    from the probe, so build the canonical URL ourselves.
    """
    ext = str(raw.get("external_id") or "")
    url = raw.get("url") or ""
    if platform == "greenhouse" and ext.isdigit():
        return f"https://boards.greenhouse.io/{slug}/jobs/{ext}"
    if platform == "lever" and ext:
        return f"https://jobs.lever.co/{slug}/{ext}"
    if platform == "ashby" and ext:
        return f"https://jobs.ashbyhq.com/{slug}/{ext}"
    if platform == "workable" and ext:
        return f"https://apply.workable.com/{slug}/j/{ext}/"
    if platform == "breezy" and ext:
        return f"https://{slug}.breezy.hr/p/{ext}"
    if platform == "personio" and ext:
        return url or f"https://{slug}.jobs.personio.com/job/{ext}"
    if platform == "rippling" and ext:
        return f"https://ats.rippling.com/{slug}/jobs/{ext}"
    return url


def lookup(session, job) -> dict | None:
    """Resolve a repost by company + title. Returns the own-link result
    fields (``platform``, ``apply_url``, ``resolved_job_id``, ``via``) or
    None when nothing matched."""
    from app.models.company import Company
    from app.services.own_link import OwnLinkError, resolve_job_from_url

    company = session.get(Company, job.company_id) if job.company_id else None
    name = (company.name if company else "") or (job.raw_json or {}).get("company_name", "")
    if not name:
        return None

    hit = find_in_catalogue(session, name, job.title)
    if hit is not None:
        return {"platform": hit.platform, "apply_url": hit.url, "resolved_job_id": str(hit.id), "via": "catalogue"}

    probe = probe_boards(name, job.title)
    if probe is None:
        from app.fetchers import FETCHER_MAP

        for platform, slug in fingerprint_company(session, job.company_id):
            try:
                jobs = FETCHER_MAP[platform]().fetch(slug) or []
            except Exception:
                continue
            raw = next((r for r in jobs if titles_match(r.get("title", ""), job.title)), None)
            if raw:
                probe = (platform, slug, raw)
                break
    if probe is None:
        return None
    platform, slug, raw = probe
    url = canonical_posting_url(platform, slug, raw)
    try:
        linked = resolve_job_from_url(url)
    except OwnLinkError as exc:
        logger.info("company_lookup: %s matched on %s/%s but own-link refused: %s", job.id, platform, slug, exc.detail)
        return {"platform": platform, "apply_url": url, "resolved_job_id": None, "via": "probe"}
    return {"platform": platform, "apply_url": url, "resolved_job_id": linked.job_id, "via": "probe"}
