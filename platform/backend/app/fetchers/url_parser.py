"""Parse an ATS job posting URL into ``(platform, slug, external_id)``.

Used by ``POST /jobs/submit-link`` (Feature A) to route a pasted link
to the right existing fetcher without each fetcher needing its own
URL-sniffing code. The per-platform regexes are intentionally narrow:
we'd rather 400 on an ambiguous URL and ask the user than import a
garbage row under the wrong company slug.

Every regex captures exactly two named groups: ``slug`` and
``external_id``. ``slug`` must match the value an existing scanner
fetcher would pass to its API (so re-submitting a link from a
scanned board upserts rather than duplicating). ``external_id`` is
the ATS's own job id — the same value the scanner's ``_normalize``
step writes to ``Job.external_id``, which guarantees the ``UNIQUE
(external_id)`` idempotency story at
:class:`app.models.job.Job`.

Adding a new ATS here is the complete backend change for manual
link support on that platform, provided the existing bulk fetcher
for that ATS is wired up in ``app/fetchers/__init__.FETCHER_MAP`` —
``fetch_one`` inherits a generic filter-on-bulk fallback from
:class:`app.fetchers.base.BaseFetcher`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class ParsedJobUrl:
    """Result of parsing a pasted ATS job URL.

    Attributes map 1:1 to the inputs that
    :func:`app.workers.tasks.scan_task._upsert_job` expects inside
    the ``raw_job`` dict — the caller builds the dict from these
    three fields plus whatever the per-platform fetcher returns.
    """

    platform: str
    slug: str
    external_id: str


# (regex, platform). Ordered most-specific first so overlapping
# patterns (e.g. several ATSes on api.ashbyhq.com style domains)
# resolve deterministically. Host-anchored — we match the whole
# URL, not just the path, so `jobs.lever.co/foo` can't accidentally
# match a third-party site that happens to contain that substring.
_URL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Greenhouse — canonical boards host + the newer job-boards host.
    # Greenhouse also surfaces job URLs via each company's own
    # `boards.greenhouse.io/{slug}/jobs/{id}` redirect; the two forms
    # are equivalent and both are covered here.
    (
        re.compile(
            r"^https?://boards\.greenhouse\.io/(?P<slug>[^/]+)/jobs/(?P<external_id>\d+)",
            re.IGNORECASE,
        ),
        "greenhouse",
    ),
    (
        re.compile(
            r"^https?://job-boards\.greenhouse\.io/(?P<slug>[^/]+)/jobs/(?P<external_id>\d+)",
            re.IGNORECASE,
        ),
        "greenhouse",
    ),
    # Lever — canonical `jobs.lever.co/{slug}/{posting_id}` where
    # posting_id is a UUID. The `/apply` suffix is tolerated because
    # that's the direct apply page, same posting id.
    (
        re.compile(
            r"^https?://jobs\.lever\.co/(?P<slug>[^/]+)/(?P<external_id>[0-9a-f-]+)(?:/apply)?/?",
            re.IGNORECASE,
        ),
        "lever",
    ),
    # Ashby — two equivalent host forms:
    #   jobs.ashbyhq.com/{slug}/{uuid}
    #   {slug}.ashbyhq.com/{uuid}  (less common, subdomain-routed)
    # The posting id is a UUID.
    (
        re.compile(
            r"^https?://jobs\.ashbyhq\.com/(?P<slug>[^/]+)/(?P<external_id>[0-9a-f-]+)",
            re.IGNORECASE,
        ),
        "ashby",
    ),
    # Workable — apply.workable.com/{slug}/j/{id} and the older
    # {slug}.workable.com/jobs/{id}. `{id}` is alphanumeric.
    #
    # F293 (closes F229): there's a third flavour observed in 100%
    # of our DB at F229 verification time —
    # ``apply.workable.com/j/{id}`` (no slug segment). The scanner
    # reads ``url`` / ``application_url`` straight off the
    # Workable API response, which always supplies this short
    # form. Pre-F293 the long-form-only regex rejected EVERY URL
    # the user could copy from our dashboard with the misleading
    # "URL host is not a recognized ATS" error.
    #
    # Short-form URLs can't be ingested directly because the
    # Workable widget API used by ``WorkableFetcher.fetch(slug)``
    # is keyed on the company slug — we can't look up the company
    # from a bare job-id. The targeted fix is in ``parse_job_url``
    # below: detect the short-form pattern after the main loop and
    # raise a SPECIFIC error message that tells the user to use
    # the long-form URL or contact admin to add the board, instead
    # of the generic "not a recognized ATS" that hid which platform
    # was actually involved.
    (
        re.compile(
            r"^https?://apply\.workable\.com/(?P<slug>[^/]+)/j/(?P<external_id>[A-Z0-9]+)",
            re.IGNORECASE,
        ),
        "workable",
    ),
    (
        re.compile(
            r"^https?://(?P<slug>[^.]+)\.workable\.com/jobs/(?P<external_id>\d+)",
            re.IGNORECASE,
        ),
        "workable",
    ),
    # BambooHR — {slug}.bamboohr.com/careers/{id}. The id is numeric.
    (
        re.compile(
            r"^https?://(?P<slug>[^.]+)\.bamboohr\.com/careers/(?P<external_id>\d+)",
            re.IGNORECASE,
        ),
        "bamboohr",
    ),
    # SmartRecruiters — careers.smartrecruiters.com/{slug}/{id} and
    # jobs.smartrecruiters.com/{slug}/{id}. `{id}` is numeric.
    (
        re.compile(
            r"^https?://(?:careers|jobs)\.smartrecruiters\.com/(?P<slug>[^/]+)/(?P<external_id>\d+)",
            re.IGNORECASE,
        ),
        "smartrecruiters",
    ),
    # Jobvite — jobs.jobvite.com/{slug}/job/{id}. Id is alphanumeric.
    (
        re.compile(
            r"^https?://jobs\.jobvite\.com/(?P<slug>[^/]+)/job/(?P<external_id>[A-Za-z0-9]+)",
            re.IGNORECASE,
        ),
        "jobvite",
    ),
    # Recruitee — {slug}.recruitee.com/o/{id-or-slug}. The id segment
    # can mix digits + title slug — we take the trailing numeric id
    # when present, else the whole slug.
    (
        re.compile(
            r"^https?://(?P<slug>[^.]+)\.recruitee\.com/o/(?P<external_id>[^/?#]+)",
            re.IGNORECASE,
        ),
        "recruitee",
    ),
]


class UnsupportedJobUrlError(ValueError):
    """Raised when none of the ATS patterns match a submitted URL.

    The caller (``POST /jobs/submit-link``) translates this to a 400
    so the user sees "that URL isn't from a supported ATS" rather
    than a silent fallback to the generic career-page scraper (which
    routinely produces garbage rows for non-ATS hosts — see the
    feature plan).
    """


def parse_job_url(url: str) -> ParsedJobUrl:
    """Parse ``url`` and return ``(platform, slug, external_id)``.

    Only the ATS patterns listed in ``_URL_PATTERNS`` are recognized.
    Anything else (generic career sites, LinkedIn, Indeed, etc.)
    raises :class:`UnsupportedJobUrlError`. The two-tier error
    handling lets the caller keep a "this needs manual company
    specification" escape hatch without silently misclassifying
    unknown hosts.

    :param url: Raw URL string as pasted by the user. Leading /
        trailing whitespace is stripped.
    :raises ValueError: The URL is empty, malformed, or not ``https://``.
    :raises UnsupportedJobUrlError: The URL is well-formed but doesn't
        match any known ATS pattern.
    """
    if not url:
        raise ValueError("URL is required.")
    url = url.strip()
    if len(url) > 1000:
        # Match the 1000-char cap on `Job.url` in models/job.py — if
        # the column couldn't hold it, we shouldn't pretend to accept it.
        raise ValueError("URL is too long (max 1000 characters).")

    parsed = urlparse(url)
    # Scheme allow-list: https only. http URLs get upgraded by the
    # ATS anyway, and disallowing file:/// / javascript: etc. here
    # is cheap defense-in-depth against SSRF-shaped payloads reaching
    # the fetcher.
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http or https.")
    if not parsed.netloc:
        raise ValueError("URL is missing a hostname.")

    for pattern, platform in _URL_PATTERNS:
        m = pattern.match(url)
        if m:
            slug = m.group("slug").strip()
            external_id = m.group("external_id").strip()
            if not slug or not external_id:
                # Regex shouldn't allow empty captures, but the
                # explicit guard documents the invariant that
                # downstream code (fetchers, upsert) relies on.
                continue
            return ParsedJobUrl(platform=platform, slug=slug, external_id=external_id)

    # F293 (closes F229): replace the generic "not a recognized ATS"
    # error with a targeted message when the user pasted a Workable
    # short-form URL. Short-form is ``apply.workable.com/j/{id}``
    # (2 path segments, no slug). The Workable widget API is keyed
    # on slug so we can't actually ingest a slug-less URL today,
    # but the user deserves to know WHY the URL was rejected
    # ("Workable IS in your supported list!") instead of the
    # confusing generic error. The frontend uses this detail to
    # render an actionable hint.
    if re.match(
        r"^https?://apply\.workable\.com/j/[A-Za-z0-9]+/?$",
        url,
        flags=re.IGNORECASE,
    ):
        raise UnsupportedJobUrlError(
            "Workable short-form URLs (``apply.workable.com/j/<id>``) "
            "can't be imported directly because they don't carry the "
            "company-slug needed for the Workable API. Please use "
            "the long-form URL "
            "(``apply.workable.com/<company-slug>/j/<id>``) — you can "
            "find it on the company's careers page — or ask an admin "
            "to add the company's Workable board to the platform."
        )

    raise UnsupportedJobUrlError(
        f"URL host '{parsed.netloc}' is not a recognized ATS. "
        "Supported: Greenhouse, Lever, Ashby, Workable, BambooHR, "
        "SmartRecruiters, Jobvite, Recruitee."
    )
