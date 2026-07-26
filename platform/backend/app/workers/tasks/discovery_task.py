"""Company discovery task -- find new companies via ATS sitemaps and slug probing."""

import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.workers.celery_app import celery_app
from app.workers.tasks._db import SyncSession
from app.models.discovery import DiscoveryRun, DiscoveredCompany
from app.models.company import Company, CompanyATSBoard
from app.utils.scan_lock import release_scan_lock

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
USER_AGENT = "JobPlatformBot/1.0 (+https://github.com/your-org/job-platform)"

# ---------------------------------------------------------------------------
# Known slug patterns to probe on various platforms
# ---------------------------------------------------------------------------

LEVER_PROBE_SLUGS = [
    "superside", "omilia", "yassir", "reap", "masabi",
    "zapier", "gitlab", "hashicorp", "elastic", "datadog",
    "snyk", "tailscale", "dbt-labs", "fly-io", "render",
    "planetscale", "grafana-labs", "temporal-technologies",
    "supabase", "airbyte", "pulumi", "cockroach-labs",
    "chainguard", "teleport", "lacework", "orca-security",
]

ASHBY_PROBE_SLUGS = [
    "superside", "omilia", "yassir", "ramp", "notion",
    "linear", "vercel", "resend", "cal-com", "plain",
    "dopt", "mintlify", "graphite", "codeium",
    "replit", "railway", "neon", "turso",
]

WELLFOUND_PROBE_SLUGS = [
    "superside", "omilia", "yassir", "zapier", "figma",
    "notion", "linear", "vercel", "supabase", "dbt-labs",
    "snyk", "tailscale", "render", "fly-io", "planetscale",
    "temporal", "pulumi", "teleport", "grafana-labs",
    "hashicorp", "gitlab", "airbyte", "neon",
]

SMARTRECRUITERS_PROBE_SLUGS = [
    "Visa", "Bosch", "SUSE", "Schwarz-Group", "Adidas",
    "DHL", "Siemens", "McDonalds", "Spotify", "Booking",
    "TUI", "Philips", "ING", "ABB",
]

# 2026-04-17 (F-fetcher-health): verified each slug live against the
# platform's API before committing. The prior lists predated several
# customer migrations — 10+ slugs across these three platforms had
# either left the ATS entirely (302→marketing site) or the customer
# closed their board. Dead slugs here propagate into DiscoveredCompany
# rows → never promoted to boards → fetcher never runs → tester sees
# "zero jobs on platform X". Ran the full survey with:
#
#   python -m pytest tests/test_fetcher_integration.py -v -m integration
#
# which hits each platform's live API and asserts non-empty for the
# seed slugs below. Dead slugs should be REMOVED here (not left as
# commented-out "maybe they'll come back") — stale-cull handles any
# slug that goes dark between checks.
RECRUITEE_PROBE_SLUGS = [
    # Verified live 2026-04-17: `bunq` (42 open), `personio` (1),
    # `adecco` (1). Older slugs (toggl / deel / huntr / remote-com /
    # omnipresent / oyster / papaya-global / lano / multiplier) all
    # redirect to recruitee.com marketing — removed.
    "bunq", "personio", "adecco",
    # Speculative — brands that are known Recruitee customers per
    # their case-study page. Probe-gate at `_probe_platform_slugs`
    # will drop them if they return empty.
    "catawiki", "parkos", "leapsome", "messagebird",
    "studiocanal", "bynder", "coolblue", "foundever",
]

BAMBOOHR_PROBE_SLUGS = [
    # Verified live 2026-04-17: `rei` (9 open). `toggl`/`aha`/`zapier`/
    # `linear`/`asana`/`dashlane`/`bluecore`/`algolia` are live tenants
    # with 0 current openings — kept because a zero today can be
    # non-zero tomorrow and the probe is cheap. Older slugs
    # (hotjar / buffer / uservoice / linode / ghost / sonatype / loom)
    # all redirect to www.bamboohr.com — tenants gone, removed.
    "rei",
    "toggl", "aha", "zapier", "linear", "asana",
    "dashlane", "bluecore", "algolia",
]

# Jobvite public API (jobs.jobvite.com/{slug}/jobs) returns 302 →
# www.jobvite.com/support/...?invalid=1 for EVERY slug probed on
# 2026-04-17. Not a slug-list issue — the platform appears to have
# retired customer-facing endpoints. Kept the list empty so the
# discovery probe short-circuits; the existing fetcher gracefully
# returns [] and the stale-board auto-deactivator culls any legacy
# jobvite boards still in the DB. Monitor for platform recovery and
# restore slugs here if/when it comes back.
JOBVITE_PROBE_SLUGS: list[str] = []

# Workday — enterprise Fortune-500 coverage. Slug is the composite
# ``{tenant}/{cluster}/{site}`` — see app/fetchers/workday.py docstring.
# All 4 verified live on 2026-04-17 via POST /wday/cxs/…/jobs with
# total job counts shown:
#   nvidia/wd5/NVIDIAExternalCareerSite   →  2000 jobs
#   salesforce/wd12/External_Career_Site  →  1417 jobs
#   citi/wd5/2                            →  2000 jobs
#   capitalone/wd12/Capital_One           →  1457 jobs
# Discovery grows this list via `app.services.ats_fingerprint`, which
# scrapes a company's public careers page and extracts myworkdayjobs.com
# URLs. For now the probe covers the 4 seed tenants; a follow-up
# task can run the fingerprinter across a curated domain list to
# bulk-add more enterprise tenants.
WORKDAY_PROBE_SLUGS = [
    "nvidia/wd5/NVIDIAExternalCareerSite",
    "salesforce/wd12/External_Career_Site",
    "citi/wd5/2",
    "capitalone/wd12/Capital_One",
]

# Himalayas /jobs/api returns HTTP 403 for every probe slug as of
# 2026-04-17. The endpoint is protected and httpx can't reach it.
# See test_fetcher_integration.BROKEN_FETCHERS. Keep empty so discovery
# doesn't waste cycles; stale-cull handles any legacy himalayas boards.
HIMALAYAS_PROBE_SLUGS: list[str] = []

# LinkedIn company page slugs (used for both RapidAPI and public search fallback)
# Focus on infra, DevOps, cloud, and security companies hiring remote
LINKEDIN_PROBE_SLUGS = [
    # Cloud / Infrastructure leaders
    "cloudflare", "datadog", "hashicorp", "elastic", "grafana-labs",
    "mongodb", "cockroach-labs", "planetscale", "neon-inc", "supabase",
    "vercel", "netlify", "render", "fly-io", "railway",
    "pulumi", "spacelift", "env0", "terraform",
    # DevOps / SRE / Platform
    "gitlab", "circleci", "buildkite", "dagger-io", "harness-io",
    "launchdarkly", "split-software", "flagsmith",
    "pagerduty", "opsgenie", "incident-io", "firehydrant",
    # Security
    "snyk", "wiz-inc", "orca-security", "lacework", "chainguard",
    "teleport", "tailscale", "crowdstrike", "palo-alto-networks",
    "fortinet", "rapid7", "tenable", "qualys",
    "sentinelone", "cyberark", "sailpoint-technologies",
    "1password", "bitwarden",
    # Remote-first / Global remote
    "zapier", "automattic", "canonical", "toptal", "remote",
    "deel", "omnipresent", "oyster-hr", "papaya-global",
    "superside", "toggl", "buffer", "doist",
    # Big tech (reference, high volume)
    "google", "microsoft", "amazon", "meta", "apple",
    "stripe", "twilio", "confluent",
]


def _crawl_greenhouse_sitemap(session, run: DiscoveryRun) -> int:
    """Crawl the Greenhouse public sitemap to discover new company board slugs."""
    sitemap_url = "https://boards.greenhouse.io/sitemap.xml"
    new_count = 0

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(sitemap_url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()

        root = ET.fromstring(resp.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = root.findall(".//sm:url/sm:loc", ns)

        seen_slugs: set[str] = set()
        for loc in urls:
            text = loc.text or ""
            parts = text.replace("https://boards.greenhouse.io/", "").split("/")
            if parts and parts[0]:
                seen_slugs.add(parts[0])

        existing = session.execute(
            select(DiscoveredCompany.slug).where(
                DiscoveredCompany.platform == "greenhouse"
            )
        ).scalars().all()
        existing_set = set(existing)

        for slug in seen_slugs:
            if slug in existing_set:
                continue
            company = DiscoveredCompany(
                id=uuid.uuid4(),
                discovery_run_id=run.id,
                name=slug.replace("-", " ").title(),
                platform="greenhouse",
                slug=slug,
                careers_url=f"https://boards.greenhouse.io/{slug}",
                status="new",
            )
            session.add(company)
            new_count += 1

        session.flush()
        logger.info("Greenhouse sitemap: %d total slugs, %d new", len(seen_slugs), new_count)

    except Exception as e:
        logger.error("Greenhouse sitemap crawl failed: %s", e)

    return new_count


def _probe_platform_slugs(session, run: DiscoveryRun, platform: str, slugs: list[str], url_template: str) -> int:
    """Generic slug prober for any platform with a public API."""
    new_count = 0
    existing = set(
        session.execute(
            select(DiscoveredCompany.slug).where(DiscoveredCompany.platform == platform)
        ).scalars().all()
    )

    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        for slug in slugs:
            if slug in existing:
                continue
            url = url_template.format(slug=slug)
            try:
                resp = client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                if resp.status_code == 200:
                    # Verify it actually returns data
                    try:
                        data = resp.json()
                        # Different platforms return different structures
                        has_data = bool(data)
                        if isinstance(data, dict):
                            has_data = bool(
                                data.get("jobs") or data.get("content") or
                                data.get("offers") or data.get("requisitions") or
                                data.get("result") or data.get("data")
                            )
                    except Exception:
                        has_data = False

                    if has_data:
                        from app.schemas.company import ATS_URL_PATTERNS
                        careers_pattern = ATS_URL_PATTERNS.get(platform, "")
                        careers_url = careers_pattern.format(slug=slug) if careers_pattern else url

                        company = DiscoveredCompany(
                            id=uuid.uuid4(),
                            discovery_run_id=run.id,
                            name=slug.replace("-", " ").title(),
                            platform=platform,
                            slug=slug,
                            careers_url=careers_url,
                            status="new",
                        )
                        session.add(company)
                        new_count += 1
            except Exception as e:
                logger.debug("%s probe failed for %s: %s", platform, slug, e)

    session.flush()
    logger.info("%s probe: %d slugs checked, %d new", platform, len(slugs), new_count)
    return new_count


# Platform API URL templates for slug probing
PLATFORM_PROBE_CONFIG = {
    "lever": {
        "slugs": LEVER_PROBE_SLUGS,
        "url": "https://api.lever.co/v0/postings/{slug}",
    },
    "ashby": {
        "slugs": ASHBY_PROBE_SLUGS,
        "url": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    },
    "wellfound": {
        "slugs": WELLFOUND_PROBE_SLUGS,
        "url": "https://wellfound.com/company/{slug}/jobs",
    },
    "smartrecruiters": {
        "slugs": SMARTRECRUITERS_PROBE_SLUGS,
        "url": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    },
    "recruitee": {
        "slugs": RECRUITEE_PROBE_SLUGS,
        "url": "https://{slug}.recruitee.com/api/offers",
    },
    "bamboohr": {
        "slugs": BAMBOOHR_PROBE_SLUGS,
        "url": "https://{slug}.bamboohr.com/careers/list",
    },
    "jobvite": {
        "slugs": JOBVITE_PROBE_SLUGS,
        "url": "https://jobs.jobvite.com/{slug}/jobs",
    },
    "himalayas": {
        "slugs": HIMALAYAS_PROBE_SLUGS,
        "url": "https://himalayas.app/jobs/api?company_slug={slug}&limit=1",
    },
}


def _probe_linkedin_slugs(session, run: DiscoveryRun) -> int:
    """Probe LinkedIn company slugs. LinkedIn doesn't return JSON, so we just
    register the slug if it's in our probe list (we know these companies exist).
    The actual job fetching happens via the LinkedInFetcher during scan time."""
    new_count = 0
    existing = set(
        session.execute(
            select(DiscoveredCompany.slug).where(DiscoveredCompany.platform == "linkedin")
        ).scalars().all()
    )

    for slug in LINKEDIN_PROBE_SLUGS:
        if slug in existing:
            continue
        company = DiscoveredCompany(
            id=uuid.uuid4(),
            discovery_run_id=run.id,
            name=slug.replace("-", " ").title(),
            platform="linkedin",
            slug=slug,
            careers_url=f"https://www.linkedin.com/company/{slug}/jobs",
            status="new",
        )
        session.add(company)
        new_count += 1

    session.flush()
    logger.info("LinkedIn probe: %d slugs registered, %d new", len(LINKEDIN_PROBE_SLUGS), new_count)
    return new_count


def _probe_workday_slugs(session, run: DiscoveryRun) -> int:
    """Register Workday slugs. Unlike the generic GET-based probe at
    :func:`_probe_platform_slugs`, Workday requires a POST with a JSON
    body to validate a slug is live, and the composite-slug shape
    (``{tenant}/{cluster}/{site}``) doesn't fit the generic URL
    template pattern. So we take the same shortcut as LinkedIn:
    ``WORKDAY_PROBE_SLUGS`` is hand-curated (verified live at seed
    time; see the docstring on that constant), so we just register
    each slug as a DiscoveredCompany without re-probing every cycle.
    The scanner's stale-board auto-deactivator will cull any entry
    that stops producing jobs over 5 consecutive cycles.

    The name shown to admins uses the tenant portion — Workday tenants
    are typically the company's short name (``nvidia``, ``salesforce``,
    etc.), which matches what a sales person expects to see.
    """
    new_count = 0
    existing = set(
        session.execute(
            select(DiscoveredCompany.slug).where(DiscoveredCompany.platform == "workday")
        ).scalars().all()
    )

    for slug in WORKDAY_PROBE_SLUGS:
        if slug in existing:
            continue
        parts = slug.split("/", 2)
        tenant = parts[0] if parts else slug
        cluster = parts[1] if len(parts) >= 2 else ""
        site = parts[2] if len(parts) >= 3 else ""
        # Public URL pointing to the tenant's career site — matches what
        # a human would land on from Google. Admins can click this from
        # /discovery to verify the board looks real before promoting.
        careers_url = (
            f"https://{tenant}.{cluster}.myworkdayjobs.com/en-US/{site}"
            if tenant and cluster and site
            else f"https://{tenant}.myworkdayjobs.com"
        )
        company = DiscoveredCompany(
            id=uuid.uuid4(),
            discovery_run_id=run.id,
            name=tenant.replace("-", " ").title(),
            platform="workday",
            slug=slug,
            careers_url=careers_url,
            status="new",
        )
        session.add(company)
        new_count += 1

    session.flush()
    logger.info("Workday probe: %d slugs registered, %d new", len(WORKDAY_PROBE_SLUGS), new_count)
    return new_count


@celery_app.task(name="app.workers.tasks.discovery_task.run_discovery", bind=True, max_retries=1)
def run_discovery(self, run_id: str | None = None):
    """Run a full discovery cycle: Greenhouse sitemap + multi-platform slug probes.

    Regression finding 186: `POST /discovery/runs` on the API side was
    creating a `DiscoveryRun` row with `status="pending"` and
    commenting "the actual discovery is executed by the background
    worker that picks up runs with status='pending'" — but no such
    polling worker existed. This task always created a fresh row
    with `status="running"` and ignored any pending rows, so every
    manual trigger from the admin UI left an orphan pending row that
    aged indefinitely (3 found aged 3+ hours during regression
    testing). Fix: accept an optional `run_id` from the API caller
    and re-use that row instead of creating a new one. The scheduled
    (cron) path still calls with no arg and creates its own row.
    """
    logger.info("Starting run_discovery run_id=%s", run_id)
    session = SyncSession()

    try:
        run = None
        if run_id:
            # F186: called from the API handler — it already inserted
            # a pending row. Flip it to running and re-use it so we
            # don't accumulate orphans.
            run = session.execute(
                select(DiscoveryRun).where(DiscoveryRun.id == run_id)
            ).scalar_one_or_none()
            if run is not None:
                run.status = "running"
                run.started_at = datetime.now(timezone.utc)
                session.flush()
            else:
                logger.warning(
                    "run_discovery called with run_id=%s but row not found — "
                    "creating a fresh row instead",
                    run_id,
                )

        if run is None:
            run = DiscoveryRun(
                id=uuid.uuid4(),
                source="scheduled",
                status="running",
            )
            session.add(run)
            session.flush()

        total_new = 0

        # Greenhouse sitemap
        gh_new = _crawl_greenhouse_sitemap(session, run)
        total_new += gh_new

        # Probe all platforms
        for platform, config in PLATFORM_PROBE_CONFIG.items():
            platform_new = _probe_platform_slugs(
                session, run, platform, config["slugs"], config["url"]
            )
            total_new += platform_new

        # LinkedIn (special handler — no JSON probe, just register known slugs)
        li_new = _probe_linkedin_slugs(session, run)
        total_new += li_new

        # Workday (special handler — POST-based API + composite slug shape
        # doesn't fit the generic GET probe. Hand-curated list; stale
        # entries culled by the scanner's auto-deactivator.)
        wd_new = _probe_workday_slugs(session, run)
        total_new += wd_new

        # Count total discovered in this run
        total_found = session.execute(
            select(DiscoveredCompany)
            .where(DiscoveredCompany.discovery_run_id == run.id)
        ).scalars().all()
        total_found_count = len(total_found)

        run.completed_at = datetime.now(timezone.utc)
        run.companies_found = total_found_count
        run.new_companies = total_new
        run.status = "completed"
        session.commit()

        logger.info(
            "run_discovery complete: %d found, %d new",
            total_found_count, total_new,
        )
        return {"found": total_found_count, "new": total_new}

    except Exception as e:
        logger.exception("run_discovery failed: %s", e)
        session.rollback()
        raise self.retry(exc=e, countdown=300)
    finally:
        session.close()


@celery_app.task(name="app.workers.tasks.discovery_task.discover_and_add_boards", bind=True, max_retries=1)
def discover_and_add_boards(self, source: str = "scheduled"):
    """Discovery + auto-add: Find new company boards and automatically add them to scanning.

    This is the separate platform discovery scan job that:
    1. Runs discovery to find new companies
    2. Automatically creates Company + CompanyATSBoard records for discovered companies
    3. Makes them immediately available for the next scan cycle

    F237(a) regression fix — the ``source`` parameter lets beat and
    the admin UI tag their rows differently:

    * Celery Beat invokes this with no args (``delay()`` without
      kwargs) → default ``source="scheduled"``. Freshness/history
      queries that filter on ``source="scheduled"`` now see these
      rows, closing the 4-day gap the tester observed (beat was
      firing; rows just got tagged ``"auto_add"`` so nothing listening
      for ``"scheduled"`` ever saw them).
    * The admin ``POST /platforms/scan/discover`` handler passes
      ``source="manual"`` explicitly so the admin-initiated runs stay
      distinguishable in the history.
    * The legacy ``source="auto_add"`` tag is retired — kept in
      existing rows for audit but no new row uses it, so we don't
      silently reinterpret historical data.
    """
    logger.info("Starting discover_and_add_boards source=%s", source)
    session = SyncSession()

    try:
        # First run discovery
        run = DiscoveryRun(
            id=uuid.uuid4(),
            source=source,
            status="running",
        )
        session.add(run)
        # F344: COMMIT the run row up front (was: flush + one big
        # commit after the whole crawl). Monitoring reported
        # discovery_freshness=stale with zero runs for 5 straight
        # days — the nightly beat was firing, but a crash anywhere
        # in the crawl rolled back the uncommitted run row, so
        # every failed night left no trace at all. With the early
        # commit, a crashed run survives as status="running" and
        # the existing fix_stuck_discovery_runs sweeper (every 6h)
        # flips it to failed — failures become visible in
        # /monitoring instead of silent.
        session.commit()

        total_new_discovered = 0
        source_errors: list[str] = []

        # F344: isolate each discovery source — one failing source
        # (e.g. the Greenhouse sitemap fetch getting blocked or
        # OOMing on a huge response) must not kill the whole run.
        # Partial discovery beats none; errors are accumulated and
        # surfaced on the run row.
        try:
            gh_new = _crawl_greenhouse_sitemap(session, run)
            total_new_discovered += gh_new
        except Exception as e:
            logger.exception("discovery source greenhouse-sitemap failed: %s", e)
            session.rollback()
            source_errors.append(f"greenhouse-sitemap: {e}")

        # Probe all platforms
        for platform, config in PLATFORM_PROBE_CONFIG.items():
            try:
                platform_new = _probe_platform_slugs(
                    session, run, platform, config["slugs"], config["url"]
                )
                total_new_discovered += platform_new
            except Exception as e:
                logger.exception("discovery source %s failed: %s", platform, e)
                session.rollback()
                source_errors.append(f"{platform}: {e}")

        # LinkedIn (special handler)
        try:
            li_new = _probe_linkedin_slugs(session, run)
            total_new_discovered += li_new
        except Exception as e:
            logger.exception("discovery source linkedin failed: %s", e)
            session.rollback()
            source_errors.append(f"linkedin: {e}")

        session.commit()

        # Now auto-add newly discovered companies as boards.
        #
        # Cap per run — `_crawl_greenhouse_sitemap` registers every slug
        # in the public sitemap (3k+ entries) without probing whether
        # each board has jobs, so an uncapped promotion would flood
        # `company_ats_boards` with 3k rows on first run. The
        # stale-board auto-deactivator (scan_task._STALE_BOARD_ZERO_
        # SCAN_THRESHOLD = 5 consecutive empty scans) eventually culls
        # dead slugs, but each empty scan burns ~30s of worker time
        # against the real scan budget. Bounding at
        # `settings.discovery_promote_batch_size` per run spreads the
        # backlog across beat ticks and keeps the first scan cycle
        # after a discovery sane.
        #
        # Platform ordering: Ashby, Lever, Greenhouse come off the
        # queue first because their APIs are the most stable and have
        # the highest conversion to live jobs in our existing corpus.
        # BambooHR + Wellfound + Himalayas last because those produce
        # the most stale-cull churn. `ORDER BY CASE platform…` lets us
        # express this inline without a separate priority column.
        from sqlalchemy import case
        from app.config import get_settings
        settings = get_settings()
        platform_priority = case(
            (DiscoveredCompany.platform == "ashby", 0),
            (DiscoveredCompany.platform == "lever", 1),
            (DiscoveredCompany.platform == "greenhouse", 2),
            (DiscoveredCompany.platform == "smartrecruiters", 3),
            (DiscoveredCompany.platform == "workable", 4),
            (DiscoveredCompany.platform == "recruitee", 5),
            (DiscoveredCompany.platform == "jobvite", 6),
            (DiscoveredCompany.platform == "bamboohr", 7),
            (DiscoveredCompany.platform == "wellfound", 8),
            (DiscoveredCompany.platform == "himalayas", 9),
            (DiscoveredCompany.platform == "linkedin", 10),
            else_=99,
        )
        new_discoveries = session.execute(
            select(DiscoveredCompany)
            .where(
                DiscoveredCompany.discovery_run_id == run.id,
                DiscoveredCompany.status == "new",
            )
            .order_by(platform_priority, DiscoveredCompany.created_at.asc())
            .limit(settings.discovery_promote_batch_size)
        ).scalars().all()

        boards_added = 0
        for disc in new_discoveries:
            # Check if board already exists
            existing_board = session.execute(
                select(CompanyATSBoard).where(
                    CompanyATSBoard.platform == disc.platform,
                    CompanyATSBoard.slug == disc.slug,
                )
            ).scalar_one_or_none()

            if existing_board:
                disc.status = "duplicate"
                continue

            # Find or create company
            company = session.execute(
                select(Company).where(Company.slug == disc.slug)
            ).scalar_one_or_none()

            if not company:
                company = Company(
                    id=uuid.uuid4(),
                    name=disc.name,
                    slug=disc.slug,
                    website=disc.careers_url or "",
                    is_target=False,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(company)
                session.flush()

            # Create ATS board
            board = CompanyATSBoard(
                id=uuid.uuid4(),
                company_id=company.id,
                platform=disc.platform,
                slug=disc.slug,
                is_active=True,
            )
            session.add(board)
            disc.status = "added"
            boards_added += 1

        run.completed_at = datetime.now(timezone.utc)
        run.companies_found = len(new_discoveries)
        run.new_companies = boards_added
        # F344: distinguish clean completion from "finished, but one
        # or more sources errored" so /monitoring history shows the
        # degradation instead of a false-green "completed".
        run.status = "completed_with_errors" if source_errors else "completed"
        session.commit()

        if source_errors:
            logger.warning(
                "discover_and_add_boards finished with %d source errors: %s",
                len(source_errors), "; ".join(source_errors)[:500],
            )
        logger.info(
            "discover_and_add_boards complete: %d discovered, %d boards added",
            len(new_discoveries), boards_added,
        )
        return {
            "discovered": len(new_discoveries),
            "boards_added": boards_added,
            "total_new_discovered": total_new_discovered,
            "source_errors": source_errors,
        }

    except Exception as e:
        logger.exception("discover_and_add_boards failed: %s", e)
        session.rollback()
        # F344: persist the failure on the (already-committed) run row
        # so the monitoring history shows WHEN and that it failed —
        # pre-fix the rollback erased the row entirely and 5 nights of
        # crashes were invisible. Best-effort: if even this write
        # fails, fall through to the retry unchanged.
        try:
            run.status = "failed"
            run.completed_at = datetime.now(timezone.utc)
            session.commit()
        except Exception:
            session.rollback()
        raise self.retry(exc=e, countdown=300)
    finally:
        session.close()
        # Finding 82: release the discovery lock acquired by
        # POST /platforms/scan/discover. 2-hour TTL safety valve
        # covers the slow-probe case; explicit release handles the
        # normal (successful / failed / retried) path.
        release_scan_lock("discover")


@celery_app.task(
    name="app.workers.tasks.discovery_task.fingerprint_existing_companies",
    bind=True,
    max_retries=1,
)
def fingerprint_existing_companies(self, limit: int = 50, only_unfingerprinted: bool = True):
    """Reverse-discovery via ATS fingerprinting.

    Walks existing ``Company`` rows that have a ``website`` set, fetches
    each company's public careers page, and runs the ATS fingerprint
    service (``app.services.ats_fingerprint.detect_ats_from_url``) to
    identify which ATS(es) the company uses. Any ``(platform, slug)``
    pair we don't already have gets added as a ``DiscoveredCompany``
    row and picked up by the existing discovery promotion path.

    Why this exists:
    The hand-curated probe lists in this module go stale every few
    months. The ATS customer-list pages (Lever /customers, Ashby
    /customers, etc.) are JS-rendered and don't yield slugs to plain
    httpx OR to headless Chromium (verified 2026-04-17 — Playwright
    hit 403 on Wellfound's DataDome guard and parsed 0 slugs from
    Lever /customers). This task flips the problem: instead of asking
    each ATS for its customers, we ask each of OUR customers what ATS
    they use. Works because most company careers pages are plain
    httpx-fetchable (no Cloudflare/DataDome on individual company
    sites, unlike on the aggregator sites).

    **Known limitation:** detection requires the ATS URL to be present
    in the *initial* HTML (pre-JS). Two categories of careers pages
    don't match that:
      * Fully client-side SPAs (Stripe stripe.com/jobs, NVIDIA's careers
        page) — initial HTML contains zero ATS strings; the embed loads
        only after JavaScript hydrates. Can't fingerprint.
      * Pages that inline the ATS URL deep in a multi-MB ``__NEXT_DATA__``
        blob past our ``max_html_bytes=6_000_000`` cap. Rare but real —
        Ramp's page has Ashby URLs at the 3.4 MB mark; anything past 6 MB
        is invisible to us.

    Expect ~30-60% detection yield depending on how modern the target
    companies' career sites are. That's still a win on a 1000-company
    corpus — hundreds of new boards discovered without hand-curation.
    Companies the fingerprinter misses still get scanned via their
    existing (ATS-side discovery) path — nothing is regressed, we just
    don't *gain* coverage on the SPAs.

    Args:
        limit: Max companies to fingerprint per task invocation. Default
            50 is ~12-15 minutes of wall-time at ~15s per HTTP fetch
            (the fingerprint service tries `/careers`, `/jobs`, `/`
            in that order per domain). Scale up via celery beat or
            explicit admin dispatches — don't crank to 1000 in one
            shot; the Celery worker is single-threaded per task.
        only_unfingerprinted: When True (default), skip companies whose
            website already produced at least one DiscoveredCompany row
            previously — avoids re-hitting the same domains every
            schedule tick. Set False to re-fingerprint everything
            (e.g. after the regex patterns change).

    Returns dict with ``{scanned, new, existing_dedup, errors}``.
    """
    from app.services.ats_fingerprint import detect_ats_from_url

    session = SyncSession()
    try:
        run = DiscoveryRun(
            id=uuid.uuid4(),
            source="fingerprint_existing",
            status="running",
        )
        session.add(run)
        session.flush()

        # Build the candidate set. When `only_unfingerprinted`, we
        # LEFT JOIN DiscoveredCompany on careers_url LIKE-matches of
        # the company.website — crude but cheap, and the false-negative
        # rate (company had a prior discovery via a different URL
        # shape) is acceptable because we dedup at the (platform, slug)
        # level below anyway.
        q = select(Company).where(Company.website != "").order_by(Company.created_at.desc())
        if only_unfingerprinted:
            # Anti-join: companies whose website doesn't appear in
            # any DiscoveredCompany.careers_url. Fast enough on ~1k
            # rows; swap to a dedicated `last_fingerprinted_at`
            # column if the corpus grows past ~50k.
            subq = select(DiscoveredCompany.careers_url)
            q = q.where(~Company.website.in_(subq))
        q = q.limit(limit)
        companies = session.execute(q).scalars().all()

        # Pre-load all (platform, slug) pairs so we can dedup without
        # a per-fingerprint DB round-trip.
        existing_pairs = set(
            (p, s)
            for p, s in session.execute(
                select(DiscoveredCompany.platform, DiscoveredCompany.slug)
            ).all()
        )

        scanned = 0
        new_count = 0
        errors = 0
        existing_dedup = 0

        for company in companies:
            scanned += 1
            # The URL that actually produced the fingerprints — we save
            # this back to Company.careers_url so future fallback work
            # (Phase C) has a concrete target without re-running the
            # fingerprinter.
            matched_url: str | None = None
            try:
                # The fingerprint service tries `/careers`, `/jobs`, `/`
                # sequentially per domain — see `detect_ats_for_domains`.
                # We call `detect_ats_from_url` three times explicitly
                # so one company's 404 on `/careers` doesn't burn the
                # whole 15s timeout waiting for network-idle.
                fps: list = []
                for suffix in ("/careers", "/jobs", ""):
                    target = company.website.rstrip("/") + suffix
                    fps = detect_ats_from_url(target, timeout=15)
                    if fps:
                        matched_url = target
                        break
            except Exception as e:
                errors += 1
                logger.warning(
                    "fingerprint failed for company %s (website=%s): %s",
                    company.name, company.website, e,
                )
                continue

            # Persist the careers URL that worked, even if no NEW ATS
            # pair was discovered this run (e.g. the company uses an
            # ATS we already knew about). Cheapest way to keep this
            # field fresh over time — the next fingerprint pass refreshes.
            # Only update if we actually found an ATS there — a careers
            # page with 0 ATS markers is of ambiguous value and we'd
            # rather leave the column NULL than store a misleading URL.
            if matched_url:
                company.careers_url = matched_url
                company.careers_url_fetched_at = datetime.now(timezone.utc)
                session.add(company)

            for fp in fps:
                key = (fp.platform, fp.slug)
                if key in existing_pairs:
                    existing_dedup += 1
                    continue
                session.add(DiscoveredCompany(
                    id=uuid.uuid4(),
                    discovery_run_id=run.id,
                    # Prefer the company's known name over the ATS slug
                    # — slugs can be abbreviated/gibberish ("abc123"
                    # style for some tenants), while `company.name` is
                    # already human-reviewed.
                    name=company.name or fp.slug.replace("-", " ").title(),
                    platform=fp.platform,
                    slug=fp.slug,
                    careers_url=fp.careers_url,
                    status="new",
                    relevance_hint=(
                        f"fingerprinted from {company.website} "
                        f"(company_id={company.id})"
                    ),
                ))
                existing_pairs.add(key)
                new_count += 1

        run.completed_at = datetime.now(timezone.utc)
        run.companies_found = scanned
        run.new_companies = new_count
        run.status = "completed"
        session.commit()

        logger.info(
            "fingerprint_existing_companies: scanned=%d, new=%d, dedup=%d, errors=%d",
            scanned, new_count, existing_dedup, errors,
        )
        return {
            "scanned": scanned,
            "new": new_count,
            "existing_dedup": existing_dedup,
            "errors": errors,
            "run_id": str(run.id),
        }

    except Exception as e:
        logger.exception("fingerprint_existing_companies failed: %s", e)
        session.rollback()
        raise self.retry(exc=e, countdown=300)
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────
# F352 — promote ATS links found in Google-Sheet rows into boards
# ─────────────────────────────────────────────────────────────────────

# ATS URL patterns → (platform, slug-extraction regex). Only platforms
# with a registered fetcher; slugs keep their original case (Ashby
# slugs are case-sensitive).
import re as _re

_SHEET_ATS_LINK_PATTERNS: list[tuple[str, "_re.Pattern[str]"]] = [
    ("greenhouse", _re.compile(r"greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", _re.I)),
    ("lever", _re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)")),
    ("ashby", _re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9._-]+)")),
    ("workable", _re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", _re.I)),
    ("smartrecruiters", _re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)")),
    ("recruitee", _re.compile(r"([a-z0-9-]+)\.recruitee\.com", _re.I)),
    ("bamboohr", _re.compile(r"([a-z0-9-]+)\.bamboohr\.com", _re.I)),
]

# Path segments that greet the slug position in non-board greenhouse
# URLs — never valid board tokens.
_SLUG_DENYLIST = {"embed", "boards", "jobs", "job", "careers", "www"}


def _extract_ats_ref(url: str) -> tuple[str, str] | None:
    """Return ``(platform, slug)`` when ``url`` points at a scrapeable
    ATS board we recognise, else None."""
    if not url:
        return None
    for platform, pattern in _SHEET_ATS_LINK_PATTERNS:
        m = pattern.search(url)
        if m:
            slug = m.group(1)
            if slug.lower() in _SLUG_DENYLIST or len(slug) < 2:
                return None
            return platform, slug
    return None


@celery_app.task(name="app.workers.tasks.discovery_task.promote_sheet_ats_links", bind=True, max_retries=1)
def promote_sheet_ats_links(self, limit: int = 100):
    """F352 — turn team-sheet job links into directly-scraped boards.

    Gap analysis on the team's sheets (2026-07-24): 33% of sheet rows
    (159 companies) linked to public boards on ATS platforms we
    already scrape — Greenhouse/Ashby/Lever/Workable/BambooHR/
    SmartRecruiters — but the boards were never registered, so the
    platform only ever saw the single row the team hand-curated
    instead of the employer's full live board.

    Daily beat (01:00 UTC, after the 00:00 scan wave ingests fresh
    sheet rows): walk ``jobs.platform='google_sheet'`` rows, extract
    ATS references from their URLs, and register any board we don't
    have yet. The next 08:00 scan wave then pulls each new board in
    full. One sheet row from Canonical → Canonical's entire
    Greenhouse board.

    Safety:
      * capped at ``limit`` new boards per run (spread large backlogs
        across days, same rationale as discovery_promote_batch_size);
      * existence-guarded per (platform, slug);
      * dead/typo'd slugs cost a few empty scans and are then culled
        by the stale-board auto-deactivator — the designed backstop
        for unprobed promotions;
      * companies are find-or-created by slug so a sheet company that
        later appears via discovery lands on the same row.
    """
    from app.models.job import Job

    session = SyncSession()
    added = 0
    seen: set[tuple[str, str]] = set()
    try:
        sheet_jobs = session.execute(
            select(Job.url).where(Job.platform == "google_sheet")
        ).scalars().all()

        for url in sheet_jobs:
            if added >= limit:
                logger.info(
                    "promote_sheet_ats_links: hit per-run cap (%d) — "
                    "remainder promotes tomorrow", limit,
                )
                break
            ref = _extract_ats_ref(url or "")
            if not ref or ref in seen:
                continue
            seen.add(ref)
            platform, slug = ref

            exists = session.execute(
                select(CompanyATSBoard.id).where(
                    CompanyATSBoard.platform == platform,
                    CompanyATSBoard.slug == slug,
                )
            ).scalar_one_or_none()
            if exists:
                continue

            cslug = _re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:200]
            company = session.execute(
                select(Company).where(Company.slug == cslug)
            ).scalar_one_or_none()
            if not company:
                company = Company(
                    id=uuid.uuid4(),
                    # Board slug as a provisional display name — the
                    # enrichment pipeline improves it later.
                    name=slug.replace("-", " ").title(),
                    slug=cslug,
                    is_target=False,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(company)
                session.flush()

            session.add(CompanyATSBoard(
                id=uuid.uuid4(),
                company_id=company.id,
                platform=platform,
                slug=slug,
                is_active=True,
            ))
            added += 1

        session.commit()
        logger.info(
            "promote_sheet_ats_links: %d new boards from %d sheet rows",
            added, len(sheet_jobs),
        )
        return {"boards_added": added, "sheet_rows": len(sheet_jobs)}
    except Exception as e:
        logger.exception("promote_sheet_ats_links failed: %s", e)
        session.rollback()
        raise self.retry(exc=e, countdown=300)
    finally:
        session.close()
