"""Resolve an aggregator posting to the employer's real application form.

F374. Himalayas is the catalogue's largest source and every one of its
rows is a repost. Its API's ``applicationLink`` is the Himalayas page
itself; that page hands the candidate to the employer through an
"Apply" redirect. Until this existed, a Himalayas job could only ever be
"needs you" with a link to the repost.

Two layers, kept apart so the pure part is testable without a network:

* ``find_apply_link(html, page_url)`` — pick the apply anchor out of the
  aggregator page (text or href says apply; off-site or a tracker path).
* ``resolve_page(page_url)`` — GET the page, follow the apply link
  through its redirects, fingerprint the final URL with ``own_link``.

Outcomes (``apply_resolve_status``) are recorded honestly so coverage
can be measured instead of guessed:

  resolved  — landed on an ATS we read; ``resolved_job_id`` set
  external  — a real employer link, but not an ATS we read (Workday…)
  unmatched — page walled AND the company + title lookup (F375) found
              nothing on the public ATS APIs
  blocked   — (transitional) page walled, lookup not yet attempted
  no_link   — page fetched, no apply anchor found (candidates logged)
  error     — network / parse failure
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# Sources whose rows are reposts of a form that lives elsewhere.
AGGREGATOR_PLATFORMS: frozenset[str] = frozenset({"himalayas", "remoteok", "remotive", "weworkremotely"})

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_TIMEOUT = 25.0

_CHALLENGE_MARKERS = ("just a moment", "cf-chl", "challenge-platform", "captcha-delivery.com", "_cf_chl_opt")


@dataclass
class Resolution:
    status: str
    apply_url: str | None = None
    platform: str | None = None
    candidates: list[str] = field(default_factory=list)
    detail: str = ""


def is_challenge(status_code: int, html: str) -> bool:
    h = (html or "")[:20000].lower()
    return status_code in (403, 429, 503) and any(m in h for m in _CHALLENGE_MARKERS)


def find_apply_link(html: str, page_url: str) -> tuple[str | None, list[str]]:
    """The apply anchor's absolute href, plus every candidate considered.

    Preference order: an off-site anchor whose text says apply; then a
    same-site anchor whose text says apply (the tracker redirect); then
    any href containing /apply. Nav links to the aggregator's own
    "post a job"/"apply now to Himalayas" pages are excluded by requiring
    the anchor to sit outside <nav>/<footer>.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    host = urlparse(page_url).netloc.lower()
    text_hits: list[tuple[bool, str]] = []
    href_hits: list[str] = []
    for a in soup.find_all("a", href=True):
        if a.find_parent(["nav", "footer", "header"]):
            continue
        href = urljoin(page_url, a["href"].strip())
        if not href.startswith("http"):
            continue
        text = " ".join(a.get_text(" ", strip=True).split()).lower()
        if re.search(r"\bapply\b", text) and not re.search(r"post a job|for employers|post job", text):
            off_site = urlparse(href).netloc.lower() != host
            text_hits.append((off_site, href))
        elif re.search(r"/apply(?:[/?#]|$)", href, re.I):
            href_hits.append(href)
    candidates = [h for _, h in text_hits] + href_hits
    for off_site, href in sorted(text_hits, key=lambda t: not t[0]):
        return href, candidates
    return (href_hits[0] if href_hits else None), candidates


def _client():
    import httpx

    return httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
                        follow_redirects=True)


def resolve_page(page_url: str) -> Resolution:
    """Fetch the aggregator page and follow its apply link (network)."""
    from app.services.own_link import parse_job_url

    try:
        with _client() as c:
            r = c.get(page_url)
            if is_challenge(r.status_code, r.text):
                return Resolution("blocked", detail=f"aggregator challenged the fetch (HTTP {r.status_code})")
            if r.status_code >= 400:
                return Resolution("error", detail=f"aggregator page HTTP {r.status_code}")
            link, candidates = find_apply_link(r.text, str(r.url))
            if not link:
                return Resolution("no_link", candidates=candidates, detail="no apply anchor on the page")
            try:
                r2 = c.get(link)
                final = str(r2.url)
                if is_challenge(r2.status_code, r2.text) and urlparse(final).netloc.lower() == urlparse(page_url).netloc.lower():
                    return Resolution("blocked", candidates=candidates, detail="apply redirect challenged")
            except Exception as exc:  # the employer side may be slow/broken; keep the link
                logger.info("aggregator: apply link follow failed for %s: %s", link, exc)
                final = link
            final = final.split("#", 1)[0]
            parsed = parse_job_url(final)
            if parsed:
                return Resolution("resolved", apply_url=final, platform=parsed.platform, candidates=candidates)
            return Resolution("external", apply_url=final, platform=_guess_platform(final), candidates=candidates)
    except Exception as exc:
        logger.warning("aggregator: resolve failed for %s", page_url, exc_info=True)
        return Resolution("error", detail=f"{type(exc).__name__}: {str(exc)[:120]}")


def _guess_platform(url: str) -> str | None:
    """Name the ATS behind an external link we can't drive, for the UI."""
    u = url.lower()
    for needle, name in (
        ("myworkdayjobs.com", "workday"), ("icims.com", "icims"), ("taleo.net", "taleo"),
        ("successfactors", "successfactors"), ("jobvite.com", "jobvite"), ("lever.co", "lever"),
        ("greenhouse.io", "greenhouse"), ("ashbyhq.com", "ashby"), ("workable.com", "workable"),
        ("smartrecruiters.com", "smartrecruiters"), ("bamboohr.com", "bamboohr"), ("recruitee.com", "recruitee"),
        ("linkedin.com", "linkedin"), ("breezy.hr", "breezy"), ("applytojob.com", "jazzhr"),
        ("rippling.com", "rippling"), ("teamtailor.com", "teamtailor"), ("personio", "personio"),
    ):
        if needle in u:
            return name
    return None


# Aggregators whose pages challenge every server fetch (F374 measured
# Himalayas at 6/6 from the VM). The page step is skipped for them
# unless a caller insists, so the company + title lookup — which never
# touches the aggregator — starts immediately.
PAGE_WALLED_AGGREGATORS: frozenset[str] = frozenset({"himalayas"})


def resolve_job(session, job, skip_page: bool = False, force_page: bool = False) -> dict:
    """Resolve one aggregator job and record the outcome on the row (sync).

    Page first (the apply redirect); when that is walled, has no link,
    or lands somewhere we can't drive, fall through to the company +
    title lookup (F375), which never touches the aggregator.
    """
    from app.services.company_lookup import lookup
    from app.services.own_link import OwnLinkError, resolve_job_from_url

    if not force_page and (skip_page or getattr(job, "platform", "") in PAGE_WALLED_AGGREGATORS):
        res = Resolution("blocked", detail="page fetch skipped: this aggregator challenges every server fetch")
    else:
        res = resolve_page(job.url)
    job.apply_resolve_status = res.status
    job.apply_resolved_at = datetime.now(timezone.utc)
    job.apply_url = res.apply_url
    job.apply_platform = res.platform
    resolved_job_id = None
    if res.status == "resolved" and res.apply_url:
        try:
            linked = resolve_job_from_url(res.apply_url)
            resolved_job_id = linked.job_id
        except OwnLinkError as exc:
            # The board exists but the posting has gone, or the board is
            # private: a real employer link we can't drive after all.
            job.apply_resolve_status = "external"
            res.detail = exc.detail
    timings: dict = {}
    if resolved_job_id is None:
        try:
            found = lookup(session, job, timings)
        except Exception:
            logger.warning("company_lookup failed for job %s", job.id, exc_info=True)
            found = None
        if found:
            job.apply_url = found["apply_url"]
            job.apply_platform = found["platform"]
            resolved_job_id = found.get("resolved_job_id")
            job.apply_resolve_status = "resolved" if resolved_job_id else "external"
            res.detail = f"matched by company + title via {found['via']}" + (f"; own-link: {found['detail']}" if found.get("detail") else "")
        elif job.apply_resolve_status == "blocked":
            job.apply_resolve_status = "unmatched"
    # What happened, on the row, so production can be read without logs.
    job.raw_json = {**(job.raw_json or {}), "apply_resolve": {"status": job.apply_resolve_status, "detail": res.detail,
                                                              "timings": timings, "at": job.apply_resolved_at.isoformat()}}
    if resolved_job_id:
        import uuid as _uuid

        job.resolved_job_id = _uuid.UUID(str(resolved_job_id))
    session.commit()
    return {
        "job_id": str(job.id),
        "status": job.apply_resolve_status,
        "apply_url": job.apply_url,
        "apply_platform": job.apply_platform,
        "resolved_job_id": str(job.resolved_job_id) if job.resolved_job_id else None,
        "candidates": res.candidates[:8],
        "detail": res.detail,
    }
