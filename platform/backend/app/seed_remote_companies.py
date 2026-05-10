"""Seed additional remote-first companies known for global remote hiring."""

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.company import Company, CompanyATSBoard

settings = get_settings()

# Remote-first companies with known ATS boards focused on infra/security/devops roles
REMOTE_COMPANIES = [
    # Greenhouse boards
    {"name": "GitLab", "platform": "greenhouse", "slug": "gitlab"},
    {"name": "Elastic", "platform": "greenhouse", "slug": "elastic"},
    {"name": "Sourcegraph", "platform": "greenhouse", "slug": "sourcegraph91"},
    {"name": "Grafana Labs", "platform": "greenhouse", "slug": "grafanalabs"},
    {"name": "Aiven", "platform": "greenhouse", "slug": "aaborisenko"},  # TODO: verify slug
    {"name": "Netlify", "platform": "greenhouse", "slug": "netlify"},
    {"name": "Mattermost", "platform": "greenhouse", "slug": "mattermost"},
    {"name": "Pulumi", "platform": "greenhouse", "slug": "pulumi"},
    {"name": "Teleport", "platform": "greenhouse", "slug": "teleport"},
    {"name": "Percona", "platform": "greenhouse", "slug": "percona"},
    {"name": "Turing", "platform": "greenhouse", "slug": "turing"},
    {"name": "Toptal", "platform": "greenhouse", "slug": "toptal"},
    {"name": "Remote.com", "platform": "greenhouse", "slug": "remotecom"},
    {"name": "Deel", "platform": "greenhouse", "slug": "deel"},
    {"name": "Oyster HR", "platform": "greenhouse", "slug": "oysterhr"},
    {"name": "Vercel", "platform": "greenhouse", "slug": "vercel"},
    {"name": "Supabase", "platform": "greenhouse", "slug": "supabase"},
    {"name": "PlanetScale", "platform": "greenhouse", "slug": "planetscale"},
    {"name": "Temporal", "platform": "greenhouse", "slug": "temporaltechnologies"},
    {"name": "Airbyte", "platform": "greenhouse", "slug": "airbyte"},
    {"name": "dbt Labs", "platform": "greenhouse", "slug": "dbtlabsinc"},
    {"name": "PostHog", "platform": "greenhouse", "slug": "posthog"},
    {"name": "Cal.com", "platform": "greenhouse", "slug": "calcom"},
    {"name": "Neon", "platform": "greenhouse", "slug": "neondatabase"},
    {"name": "CrowdStrike", "platform": "greenhouse", "slug": "crowdstrike"},
    {"name": "Snyk", "platform": "greenhouse", "slug": "snyk"},
    {"name": "Lacework", "platform": "greenhouse", "slug": "lacework"},
    {"name": "Wiz", "platform": "greenhouse", "slug": "wizinc"},
    {"name": "SentinelOne", "platform": "greenhouse", "slug": "sentinelone"},
    {"name": "1Password", "platform": "greenhouse", "slug": "1password"},
    {"name": "Bitwarden", "platform": "greenhouse", "slug": "bitwarden"},
    # F336 — Figma + Notion are seeded on Wellfound elsewhere in
    # this file but Wellfound's GraphQL endpoint is Cloudflare /
    # DataDome blocked from datacenter IPs (see
    # app/fetchers/wellfound.py docstring), so those rows produce
    # zero jobs. Both companies actually post to Greenhouse — adding
    # the working ATS slugs here so the boards start producing
    # immediately. The dead Wellfound rows will auto-deactivate
    # after 5 consecutive zero scans (Finding 7 + F330 surface).
    #
    # Slugs verified via the public Greenhouse pattern
    # ``boards.greenhouse.io/<slug>``. If a slug ever drifts
    # (Notion has migrated ATS at least once), the F330 dark-
    # platform observability surface will catch it within a couple
    # of scan cycles.
    {"name": "Figma", "platform": "greenhouse", "slug": "figma"},
    {"name": "Notion", "platform": "greenhouse", "slug": "notion"},

    # Lever boards
    {"name": "Zapier", "platform": "lever", "slug": "zapier"},
    {"name": "DigitalOcean", "platform": "lever", "slug": "digitalocean"},
    {"name": "CircleCI", "platform": "lever", "slug": "circleci"},
    {"name": "Kong", "platform": "lever", "slug": "kong"},
    {"name": "Automattic", "platform": "lever", "slug": "automattic"},
    {"name": "InVision", "platform": "lever", "slug": "invisionapp"},
    {"name": "Loom", "platform": "lever", "slug": "useloom"},
    {"name": "Auth0", "platform": "lever", "slug": "auth0"},
    {"name": "Heroku", "platform": "lever", "slug": "heroku"},
    {"name": "Render", "platform": "lever", "slug": "render"},
    {"name": "Fly.io", "platform": "lever", "slug": "fly-io"},
    {"name": "Railway", "platform": "lever", "slug": "railway"},
    {"name": "Spacelift", "platform": "lever", "slug": "spacelift"},
    {"name": "Env0", "platform": "lever", "slug": "env0"},
    {"name": "Oxeye", "platform": "lever", "slug": "oxeye"},

    # Ashby boards
    {"name": "Tailscale", "platform": "ashby", "slug": "tailscale"},
    {"name": "Fly.io", "platform": "ashby", "slug": "fly-io"},
    {"name": "Linear", "platform": "ashby", "slug": "linear"},
    {"name": "Resend", "platform": "ashby", "slug": "resend"},
    {"name": "Tinybird", "platform": "ashby", "slug": "tinybird"},
    {"name": "Axiom", "platform": "ashby", "slug": "axiom"},
    {"name": "Stainless", "platform": "ashby", "slug": "stainlessapi"},

    # SmartRecruiters boards (slug = company identifier)
    {"name": "Visa", "platform": "smartrecruiters", "slug": "Visa"},
    {"name": "SUSE", "platform": "smartrecruiters", "slug": "SUSE"},
    {"name": "Spotify", "platform": "smartrecruiters", "slug": "Spotify"},
    {"name": "Booking.com", "platform": "smartrecruiters", "slug": "Booking"},
    {"name": "Adidas", "platform": "smartrecruiters", "slug": "Adidas"},
    {"name": "Siemens", "platform": "smartrecruiters", "slug": "Siemens"},
    {"name": "DHL", "platform": "smartrecruiters", "slug": "DHL"},
    {"name": "Bosch", "platform": "smartrecruiters", "slug": "Bosch"},
    {"name": "Philips", "platform": "smartrecruiters", "slug": "Philips"},
    {"name": "ABB", "platform": "smartrecruiters", "slug": "ABB"},

    # Recruitee boards (slug = subdomain)
    {"name": "Superside", "platform": "recruitee", "slug": "superside"},
    {"name": "Huntr", "platform": "recruitee", "slug": "huntr"},
    {"name": "Toggl", "platform": "recruitee", "slug": "toggl"},
    {"name": "Omnipresent", "platform": "recruitee", "slug": "omnipresent"},
    {"name": "Lano", "platform": "recruitee", "slug": "lano"},
    {"name": "Multiplier", "platform": "recruitee", "slug": "multiplier"},
    {"name": "Oyster HR", "platform": "recruitee", "slug": "oyster"},
    {"name": "Velocity Global", "platform": "recruitee", "slug": "velocity-global"},

    # BambooHR boards (slug = subdomain)
    {"name": "Toggl", "platform": "bamboohr", "slug": "toggl"},
    {"name": "Hotjar", "platform": "bamboohr", "slug": "hotjar"},
    {"name": "Buffer", "platform": "bamboohr", "slug": "buffer"},
    {"name": "UserVoice", "platform": "bamboohr", "slug": "uservoice"},
    {"name": "Aha!", "platform": "bamboohr", "slug": "aha"},

    # Jobvite boards (slug = company path segment)
    {"name": "Twilio", "platform": "jobvite", "slug": "twilio"},
    {"name": "Zendesk", "platform": "jobvite", "slug": "zendesk"},
    {"name": "Unity", "platform": "jobvite", "slug": "unity"},
    {"name": "PagerDuty", "platform": "jobvite", "slug": "pagerduty"},
    {"name": "Rapid7", "platform": "jobvite", "slug": "rapid7"},

    # Wellfound boards (slug = company handle, uses GraphQL API)
    {"name": "Superside", "platform": "wellfound", "slug": "superside"},
    {"name": "Notion", "platform": "wellfound", "slug": "notion"},
    {"name": "Linear", "platform": "wellfound", "slug": "linear"},
    {"name": "Vercel", "platform": "wellfound", "slug": "vercel"},
    {"name": "Supabase", "platform": "wellfound", "slug": "supabase"},
    {"name": "Snyk", "platform": "wellfound", "slug": "snyk"},
    {"name": "Zapier", "platform": "wellfound", "slug": "zapier"},
    {"name": "Figma", "platform": "wellfound", "slug": "figma"},
    {"name": "dbt Labs", "platform": "wellfound", "slug": "dbt-labs"},
    {"name": "Tailscale", "platform": "wellfound", "slug": "tailscale"},

    # Himalayas boards (aggregator API - __all__ fetches all remote jobs)
    {"name": "Himalayas Remote", "platform": "himalayas", "slug": "__all__"},

    # WeWorkRemotely (aggregator - __all__ fetches across all WWR categories)
    {"name": "WeWorkRemotely", "platform": "weworkremotely", "slug": "__all__"},

    # RemoteOK (aggregator - __all__ fetches all remote jobs)
    {"name": "RemoteOK", "platform": "remoteok", "slug": "__all__"},

    # Remotive (aggregator - __all__ fetches all remote jobs)
    {"name": "Remotive", "platform": "remotive", "slug": "__all__"},

    # HackerNews "Who is hiring?" monthly thread — single synthetic
    # board; each comment in the thread is a job posting from a
    # different hirer. The fetcher finds the latest thread itself
    # (no per-month re-seeding needed) and a Redis descendants-count
    # marker short-circuits repeat scans when the thread hasn't grown.
    {"name": "HN Who's Hiring", "platform": "hackernews", "slug": "__all__"},

    # YC Work at a Startup — two-stage fetcher joining yc-oss batch
    # dumps (company metadata) with workatastartup.com/jobs/search
    # (postings). Single synthetic board; per-job Company rows
    # created from `companySlug`.
    {"name": "YC Work at a Startup", "platform": "yc_waas", "slug": "__all__"},

    # F335 — Working Nomads (workingnomads.com). Curated remote
    # aggregator with strong infra/devops/SRE cohort. Public RSS
    # feed at workingnomads.com/jobsrss; same single-synthetic-
    # board pattern (slug='__all__') as WWR / RemoteOK / Remotive.
    # Per-item company resolved by the fetcher (dc:creator → desc-
    # body regex → "unknown" cascade).
    {"name": "Working Nomads", "platform": "workingnomads", "slug": "__all__"},
]


def slugify(name: str) -> str:
    return name.lower().replace(" ", "-").replace(".", "-").replace("_", "-").strip("-")


async def seed_remote():
    """Seed the curated remote-friendly company + ATS-board list.

    F270 — pre-fix this lookup was by ``name`` only, but the unique
    constraint on ``companies.slug`` caused IntegrityError on every
    backend startup when a company existed in the DB under a slightly
    different name (e.g. scan added ``"Vercel, Inc."`` with slug
    ``"vercel-inc"``; seed then tries to create ``"Vercel"`` with slug
    ``"vercel"`` — no name collision but if discovery had also added
    ``"Vercel"`` with slug ``"vercel"``, the seed's INSERT collides).
    Result: every backend container restart logged a giant traceback
    marked "(non-fatal)" — noisy + masks real errors.

    Fix:
      1. Lookup by **both name AND slug** so we reuse the existing row
         no matter which side matches.
      2. Wrap the per-company INSERT in a SAVEPOINT (``begin_nested``).
         If a slug-conflict still slips through (e.g. concurrent
         insert from a scan), we catch the IntegrityError, rollback
         the savepoint, re-lookup by slug, and reuse the conflict
         row. The outer transaction continues — no loud traceback,
         no lost work on subsequent companies in the loop.
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import or_

    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        added = 0
        skipped = 0
        recovered = 0  # F270: counter for slug-conflict recoveries

        for entry in REMOTE_COMPANIES:
            name = entry["name"]
            platform = entry["platform"]
            slug = entry["slug"]
            company_slug = slugify(name)

            # F270 — match on EITHER name OR computed slug. Pre-fix,
            # the lookup only matched name; if a scan or earlier seed
            # had inserted the same slug under a different name, we'd
            # try to create a duplicate slug and fail.
            result = await session.execute(
                select(Company).where(
                    or_(Company.name == name, Company.slug == company_slug)
                )
            )
            company = result.scalar_one_or_none()
            if not company:
                # Wrap the INSERT in a savepoint so a residual race
                # (concurrent scan adding the same slug between our
                # SELECT and INSERT) doesn't poison the outer
                # transaction. On conflict we rollback the savepoint,
                # re-fetch by slug, and continue with that row.
                try:
                    async with session.begin_nested():
                        company = Company(
                            id=uuid.uuid4(),
                            name=name,
                            slug=company_slug,
                            is_target=False,
                            created_at=datetime.now(timezone.utc),
                        )
                        session.add(company)
                        await session.flush()
                except IntegrityError:
                    # Race lost — someone else inserted the slug.
                    # Reload from DB and reuse.
                    result = await session.execute(
                        select(Company).where(Company.slug == company_slug)
                    )
                    company = result.scalar_one()
                    recovered += 1

            # Check if board exists
            result = await session.execute(
                select(CompanyATSBoard).where(
                    CompanyATSBoard.company_id == company.id,
                    CompanyATSBoard.platform == platform,
                    CompanyATSBoard.slug == slug,
                )
            )
            if result.scalar_one_or_none():
                skipped += 1
                continue

            session.add(CompanyATSBoard(
                id=uuid.uuid4(),
                company_id=company.id,
                platform=platform,
                slug=slug,
                is_active=True,
            ))
            added += 1

        await session.commit()
        print(
            f"Added {added} new boards, skipped {skipped} existing"
            + (f", recovered {recovered} slug conflicts" if recovered else "")
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_remote())
