from app.fetchers.greenhouse import GreenhouseFetcher
from app.fetchers.lever import LeverFetcher
from app.fetchers.ashby import AshbyFetcher
from app.fetchers.workable import WorkableFetcher
from app.fetchers.bamboohr import BambooHRFetcher
from app.fetchers.himalayas import HimalayasFetcher
from app.fetchers.wellfound import WellfoundFetcher
from app.fetchers.jobvite import JobviteFetcher
from app.fetchers.smartrecruiters import SmartRecruitersFetcher
from app.fetchers.recruitee import RecruiteeFetcher
from app.fetchers.workday import WorkdayFetcher
from app.fetchers.career_page import CareerPageFetcher
from app.fetchers.weworkremotely import WeWorkRemotelyFetcher
from app.fetchers.remoteok import RemoteOKFetcher
from app.fetchers.remotive import RemotiveFetcher
from app.fetchers.linkedin import LinkedInFetcher
from app.fetchers.hackernews import HackerNewsFetcher
from app.fetchers.yc_waas import YCWaaSFetcher
from app.fetchers.adzuna import AdzunaFetcher
from app.fetchers.workingnomads import WorkingNomadsFetcher
from app.fetchers.google_sheet import GoogleSheetFetcher

FETCHER_MAP = {
    "greenhouse": GreenhouseFetcher,
    "lever": LeverFetcher,
    "ashby": AshbyFetcher,
    "workable": WorkableFetcher,
    "bamboohr": BambooHRFetcher,
    "himalayas": HimalayasFetcher,
    "wellfound": WellfoundFetcher,
    "jobvite": JobviteFetcher,
    "smartrecruiters": SmartRecruitersFetcher,
    "recruitee": RecruiteeFetcher,
    # Workday — enterprise coverage (Fortune-500 tenants). Slug is a
    # composite `{tenant}/{cluster}/{site}` — see app.fetchers.workday
    # module docstring for why.
    "workday": WorkdayFetcher,
    "weworkremotely": WeWorkRemotelyFetcher,
    "remoteok": RemoteOKFetcher,
    "remotive": RemotiveFetcher,
    "linkedin": LinkedInFetcher,
    # HN "Who is hiring?" monthly thread — aggregator; slug is
    # always `__all__`. See app/fetchers/hackernews.py.
    "hackernews": HackerNewsFetcher,
    # Y Combinator Work at a Startup — aggregator, slug `__all__`.
    # Two-stage fetcher: yc-oss batch dumps for company enumeration
    # + workatastartup.com /jobs/search for postings. See
    # app/fetchers/yc_waas.py.
    "yc_waas": YCWaaSFetcher,
    # Adzuna — country-scoped aggregator. Slug = ISO-3166 alpha-2
    # country code (``ae`` for UAE, ``us``, ``gb``, ``in``, etc.).
    # Requires ADZUNA_APP_ID + ADZUNA_APP_KEY env vars. See
    # app/fetchers/adzuna.py for the rationale + per-board limits.
    "adzuna": AdzunaFetcher,
    # F335 — Working Nomads RSS aggregator, slug `__all__`. Bugfix:
    # the fetcher + PlatformFilter entry + seeded board all shipped,
    # but this map registration was missed — every scan tick errored
    # "No fetcher for platform: workingnomads" (21 errors/week, zero
    # jobs sourced) until it was added here.
    "workingnomads": WorkingNomadsFetcher,
    # F351 — team-curated Google Sheets as a scan source. Slug = the
    # sheet URL or ID (link-shared as Anyone-with-link → Viewer; no
    # Google API key needed). Register sheets via the admin Platforms
    # page → Add Board. See app/fetchers/google_sheet.py for the
    # expected column headers.
    "google_sheet": GoogleSheetFetcher,
}
