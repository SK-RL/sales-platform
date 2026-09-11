"""Bring your own link — resolve a pasted job URL into a Job we can drive.

F371. Tsenta's "Add your own link" accepts any URL and then quietly
leaves Apply enabled even when it understood nothing (we watched it do
that with a garbage link). Ours enforces what it says: a link either
resolves to a posting on an ATS we can read — and becomes a normal
``Job`` the review queue can prepare, extract and (where we have a
submitter) submit — or it is refused with the reason, and nothing is
created.

Two steps, both pure enough to test:

* ``parse_job_url``  — recognise the posting URL shape of each ATS we
  extract from and pull out ``(platform, board slug, posting token)``.
  Only public hosted-board URLs: an embedded Greenhouse form on a
  company site (``?gh_jid=``) or a Workable search-page link
  (``jobs.workable.com/view/…``) does not carry the board slug, so it is
  refused rather than guessed.
* ``resolve_job_from_url`` — find the posting in our catalogue by
  ``(platform, external_id)``, or fetch the board with the same fetcher
  the scanner uses, upsert the one matching posting through the
  scanner's own ``_upsert_job`` (so scoring, geography and role
  matching are identical to a scanned job), creating the company/board
  rows if this is a board we never scanned.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select

logger = logging.getLogger(__name__)


class OwnLinkError(Exception):
    """A refusal with an HTTP status and a reason the UI shows verbatim."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ParsedJobUrl:
    platform: str
    slug: str
    token: str
    # Set when the URL token IS the fetcher's external_id; None when the
    # posting has to be matched by URL (Recruitee URLs carry a slug, the
    # API an id).
    external_id: str | None


# Posting-URL shapes, verified against live boards. The token group is
# the part that identifies the posting on that board.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(r"^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([^/?#]+)/jobs/(\d+)", re.I)),
    ("lever", re.compile(r"^https?://jobs(?:\.eu)?\.lever\.co/([^/?#]+)/([0-9a-f]{8}-[0-9a-f-]{27})", re.I)),
    ("ashby", re.compile(r"^https?://jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f]{8}-[0-9a-f-]{27})", re.I)),
    ("workable", re.compile(r"^https?://apply\.workable\.com/([^/?#]+)/j/([A-Za-z0-9]+)", re.I)),
    ("recruitee", re.compile(r"^https?://(?!www\.|api\.|jobs\.)([a-z0-9-]+)\.recruitee\.com/o/([^/?#]+)", re.I)),
    ("breezy", re.compile(r"^https?://(?!www\.|app\.|api\.)([a-z0-9-]+)\.breezy\.hr/p/([0-9a-f]+)", re.I)),
    ("personio", re.compile(r"^https?://([a-z0-9-]+)\.jobs\.personio\.(?:com|de)/job/(\d+)", re.I)),
    ("rippling", re.compile(r"^https?://ats\.rippling\.com/([^/?#]+)/jobs/([0-9a-f]{8}-[0-9a-f-]{27})", re.I)),
    ("teamtailor", re.compile(r"^https?://(?!www\.|app\.|api\.)([a-z0-9-]+)(?:\.[a-z]{2})?\.teamtailor\.com/jobs/(\d+)", re.I)),
    ("jazzhr", re.compile(r"^https?://(?!www\.|app\.)([a-z0-9-]+)\.applytojob\.com/apply/([A-Za-z0-9]{6,})(?:[/?#]|$)", re.I)),
    ("bamboohr", re.compile(r"^https?://(?!www\.|api\.|jobs\.)([a-z0-9-]+)\.bamboohr\.com/careers/(\d+)", re.I)),
    ("smartrecruiters", re.compile(r"^https?://jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)", re.I)),
    # F376 — an aggregator repost. Resolves to the repost row (created
    # from the Himalayas API by company slug when we never scanned it);
    # the endpoint then runs the company + title resolver (F375) to reach
    # the employer's form.
    ("himalayas", re.compile(r"^https?://(?:www\.)?himalayas\.app/companies/([^/?#]+)/jobs/([^/?#]+)", re.I)),
)

# Shapes we recognise but cannot resolve — refused with a specific reason.
_KNOWN_UNRESOLVABLE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[?&]gh_jid=\d+", re.I),
     "That's a Greenhouse form embedded in a company site; paste the boards.greenhouse.io link for the posting instead."),
    (re.compile(r"^https?://jobs\.workable\.com/", re.I),
     "That's a Workable search page; paste the apply.workable.com link for the posting instead."),
    (re.compile(r"myworkdayjobs\.com", re.I),
     "Workday postings need an account on the employer's site, which we can't create for you."),
    (re.compile(r"linkedin\.com/jobs", re.I),
     "LinkedIn needs you signed in; open the posting's own apply link and paste that."),
)

_SUPPORTED_HINT = (
    "Paste a posting link from Greenhouse, Lever, Ashby, Workable, Recruitee, "
    "BambooHR, SmartRecruiters, Breezy, Personio, Rippling, JazzHR, Teamtailor, or a Himalayas job page."
)


def _external_id(platform: str, slug: str, token: str) -> str | None:
    """The fetcher's external_id for this token, mirroring each fetcher."""
    if platform in ("greenhouse", "lever", "ashby", "workable", "breezy", "personio", "rippling", "jazzhr", "teamtailor"):
        return token
    if platform == "bamboohr":
        return f"bamboo-{slug}-{token}"
    if platform == "smartrecruiters":
        return f"sr-{token}"
    if platform == "himalayas":
        return f"himalayas-{token}"  # fetcher: guid's last path segment
    return None  # recruitee: URL slug ≠ API id


def parse_job_url(url: str) -> ParsedJobUrl | None:
    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    for platform, pattern in _PATTERNS:
        m = pattern.match(u)
        if m:
            slug, token = m.group(1), m.group(2)
            if platform == "smartrecruiters":
                slug = slug  # SR board slugs are case-sensitive; keep as typed
            else:
                slug = slug.lower()
            return ParsedJobUrl(platform, slug, token, _external_id(platform, slug, token))
    return None


def refusal_for(url: str) -> str:
    """Why a URL that didn't parse is refused."""
    u = (url or "").strip()
    for pattern, why in _KNOWN_UNRESOLVABLE:
        if pattern.search(u):
            return why
    return "That doesn't look like a job posting link we can read. " + _SUPPORTED_HINT


@dataclass
class ResolvedJob:
    job_id: str
    platform: str
    slug: str
    external_id: str
    title: str
    company_name: str
    url: str
    created: bool


def _fetch_board(platform: str, slug: str) -> list[dict]:
    from app.fetchers import FETCHER_MAP

    cls = FETCHER_MAP.get(platform)
    if cls is None:
        raise OwnLinkError(422, f"We can't read {platform} boards.")
    return cls().fetch(slug) or []


def _matches(raw: dict, parsed: ParsedJobUrl) -> bool:
    if parsed.external_id:
        return str(raw.get("external_id", "")) == parsed.external_id
    # Recruitee: the URL slug appears in the posting's own URL.
    return f"/o/{parsed.token}".lower() in str(raw.get("url", "")).lower()


def resolve_job_from_url(url: str) -> ResolvedJob:
    """Turn a pasted URL into a catalogue Job, or raise OwnLinkError."""
    from app.models.company import Company, CompanyATSBoard
    from app.models.job import Job
    from app.workers.tasks._db import SyncSession
    from app.workers.tasks.scan_task import _upsert_job

    parsed = parse_job_url(url)
    if parsed is None:
        raise OwnLinkError(422, refusal_for(url))

    session = SyncSession()
    try:
        job = None
        if parsed.external_id:
            job = session.execute(
                select(Job).where(Job.platform == parsed.platform, Job.external_id == parsed.external_id)
                .order_by(Job.first_seen_at.desc())
            ).scalars().first()
        if job is None:
            needle = f"%/o/{parsed.token}%" if parsed.platform == "recruitee" else None
            if needle:
                job = session.execute(
                    select(Job).where(Job.platform == "recruitee", Job.url.ilike(needle))
                    .order_by(Job.first_seen_at.desc())
                ).scalars().first()
        if job is not None:
            company = session.get(Company, job.company_id)
            return ResolvedJob(str(job.id), job.platform, parsed.slug, job.external_id, job.title,
                               company.name if company else "", job.url, created=False)

        try:
            raw_jobs = _fetch_board(parsed.platform, parsed.slug)
        except OwnLinkError:
            raise
        except Exception as exc:  # network, 404 board, malformed payload
            logger.warning("own_link: board fetch failed for %s/%s", parsed.platform, parsed.slug, exc_info=True)
            raise OwnLinkError(502, f"Couldn't read the {parsed.platform} board '{parsed.slug}' right now ({type(exc).__name__}).")

        raw = next((r for r in raw_jobs if _matches(r, parsed)), None)
        if raw is None and parsed.platform == "himalayas":
            # Its API ignores an unknown company slug and returns the
            # global feed, so "the board has postings but not this one"
            # would be a lie here.
            raise OwnLinkError(404, "Couldn't find that job on Himalayas — check the link, or the posting may have been removed.")
        if raw is None:
            raise OwnLinkError(
                404,
                f"That posting isn't on the {parsed.platform} board '{parsed.slug}' any more — it may have closed."
                if raw_jobs else
                f"The {parsed.platform} board '{parsed.slug}' has no public postings (wrong slug, or a private board).",
            )

        board = session.execute(
            select(CompanyATSBoard).where(
                CompanyATSBoard.platform == parsed.platform, CompanyATSBoard.slug == parsed.slug
            )
        ).scalars().first()
        if board is not None:
            company = session.get(Company, board.company_id)
        else:
            name = (raw.get("company_name") or (raw.get("raw_json") or {}).get("company_name") or parsed.slug).strip()
            company_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or parsed.slug
            # By name OR slug: an aggregator scan may already have created
            # "greenbone-ag" under a different display name, and
            # companies.slug is unique — production 500'd on exactly this
            # ("The posting was read but could not be saved").
            company = session.execute(
                select(Company).where((Company.name == name) | (Company.slug == company_slug))
            ).scalars().first()
            if company is None:
                company = Company(id=uuid.uuid4(), name=name, slug=company_slug, is_target=False)
                session.add(company)
                session.flush()
            board = CompanyATSBoard(id=uuid.uuid4(), company_id=company.id, platform=parsed.platform,
                                    slug=parsed.slug, is_active=True)
            session.add(board)
            session.flush()

        # Same scoring inputs the scanner uses (F307), so a pasted posting
        # scores like a scanned one. Best-effort: a missing config just
        # means the scorer's defaults, which the nightly rescore corrects.
        cluster_config = approved = None
        try:
            from app.workers.tasks import scan_task as _st

            cluster_config = _st.load_cluster_config_sync(session)
            approved = _st._approved_roles_set_from_config(cluster_config)
        except Exception:
            cluster_config = approved = None
        action = _upsert_job(session, company, board, raw, cluster_config=cluster_config, approved_roles_set=approved)
        if action == "skipped":
            raise OwnLinkError(422, "That posting was rejected by our job filters (empty or placeholder title).")
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.warning("own_link: commit failed for %s/%s", parsed.platform, parsed.slug, exc_info=True)
            raise OwnLinkError(500, f"The posting was read but could not be saved ({type(exc).__name__}).")

        job = session.execute(
            select(Job).where(Job.platform == parsed.platform, Job.external_id == str(raw.get("external_id")))
            .order_by(Job.first_seen_at.desc())
        ).scalars().first()
        if job is None:
            raise OwnLinkError(500, "The posting was read but could not be saved.")
        return ResolvedJob(str(job.id), job.platform, parsed.slug, job.external_id, job.title,
                           company.name, job.url, created=(action == "new"))
    except OwnLinkError:
        session.rollback()
        raise
    finally:
        session.close()
