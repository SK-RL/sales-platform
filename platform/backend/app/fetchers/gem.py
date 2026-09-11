"""Fetch open positions from Gem job boards (jobs.gem.com).

F391. Public GraphQL, no key: ``POST https://jobs.gem.com/api/public/
graphql/batch`` with the ``JobBoardList`` operation (``boardId`` = the
board's vanity path) lists ``jobPostings`` with ``extId``, ``title``,
``locations`` and ``job{department, locationType, employmentType}``;
``ExternalJobPosting`` (``boardId``, ``extId``) returns one posting plus
``oatsJobPostFieldsAndQuestions`` — the application form's fixed fields
and custom questions. Verified on modular and gem with a plain HTTP
client (no cookies needed).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.fetchers.base import BaseFetcher

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://jobs.gem.com/api/public/graphql/batch"
POSTING_URL = "https://jobs.gem.com/{slug}/{ext_id}"

LIST_QUERY = """query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      locations { id name city isoCountry isRemote extId __typename }
      job { id department { id name extId __typename } locationType employmentType __typename }
      __typename
    }
    __typename
  }
}
"""

POSTING_QUERY = """fragment PublicQuestionFragment on PublicOatsQuestion {
  extId answerType displayType fileType text description isRequired
  options { extId value __typename }
  numericRatingRange __typename
}
fragment PublicDemographicQuestionFragment on PublicOatsDemographicSurveyQuestion {
  extId answerType displayType text description
  options { extId value __typename }
  __typename
}
query ExternalJobPosting($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id title extId firstPublishedTsSec isApplicationFormHidden isUnlistedExternally
    locations { id extId name city isoCountry isRemote __typename }
    job { id locationType employmentType requisitionId teamDisplayName department { id extId name __typename } __typename }
    compensationHtml __typename
  }
  oatsJobPostFieldsAndQuestions(jobBoardVanityPath: $boardId, jobPostExtId: $extId) {
    fields { fieldType isRequired __typename }
    questions { ...PublicQuestionFragment __typename }
    demographicSurvey { extId surveyType questions { ...PublicDemographicQuestionFragment __typename } __typename }
    __typename
  }
}
"""


def graphql(client: httpx.Client, operation: str, query: str, variables: dict) -> dict:
    resp = client.post(GRAPHQL_URL, json=[{"operationName": operation, "variables": variables, "query": query}],
                       headers={"Accept": "application/json", "Content-Type": "application/json"})
    resp.raise_for_status()
    body = resp.json()
    first = body[0] if isinstance(body, list) and body else body
    if not isinstance(first, dict) or first.get("errors"):
        raise ValueError(f"Gem GraphQL error: {str(first)[:200]}")
    return first.get("data") or {}


class GemFetcher(BaseFetcher):
    PLATFORM = "gem"

    def fetch(self, slug: str) -> list[dict]:
        client = self._get_client()
        try:
            data = graphql(client, "JobBoardList", LIST_QUERY, {"boardId": slug})
        except httpx.HTTPStatusError as exc:
            logger.warning("Gem %s returned %s", slug, exc.response.status_code)
            return []
        except (httpx.RequestError, ValueError) as exc:
            logger.warning("Gem %s request failed: %s", slug, exc)
            return []
        postings = ((data.get("oatsExternalJobPostings") or {}).get("jobPostings")) or []
        jobs = [j for j in (self._normalize(p, slug) for p in postings if isinstance(p, dict)) if j]
        logger.info("Gem %s fetched %d postings", slug, len(jobs))
        return jobs

    def fetch_one(self, slug: str, external_id: str) -> dict | None:
        ids = self._id_forms(external_id)
        ext = next((i for i in ids if not i.startswith("gem-")), "")
        if not ext:
            return None
        try:
            data = self.posting(slug, ext)
        except (httpx.HTTPError, ValueError):
            return None
        raw = data.get("oatsExternalJobPosting")
        if not raw:
            return None
        job = self._normalize(raw, slug)
        if job:
            job["raw_json"]["form"] = data.get("oatsJobPostFieldsAndQuestions")
        return job

    def posting(self, slug: str, ext_id: str) -> dict:
        return graphql(self._get_client(), "ExternalJobPosting", POSTING_QUERY, {"boardId": slug, "extId": ext_id})

    def _normalize(self, raw: dict[str, Any], slug: str) -> dict | None:
        ext = str(raw.get("extId") or "")
        title = (raw.get("title") or "").strip()
        if not ext or not title or raw.get("isApplicationFormHidden"):
            return None
        job = raw.get("job") or {}
        locs = raw.get("locations") or job.get("locations") or []
        names = [(l.get("name") or l.get("city") or "").strip() for l in locs if isinstance(l, dict)]
        names = [n for n in names if n]
        remote = (job.get("locationType") or "").upper() == "REMOTE" or any(l.get("isRemote") for l in locs if isinstance(l, dict))
        location_raw = "; ".join(dict.fromkeys(names))
        if remote and "remote" not in location_raw.lower():
            location_raw = "Remote" + (f" ({location_raw})" if location_raw else "")
        dept = job.get("department") or {}
        return {
            "external_id": f"gem-{ext}",
            "company_slug": slug,
            "title": title,
            "url": POSTING_URL.format(slug=slug, ext_id=ext),
            "platform": self.PLATFORM,
            "location_raw": location_raw,
            "remote_scope": "remote" if remote else "",
            "department": (dept.get("name") if isinstance(dept, dict) else "") or "",
            "employment_type": (job.get("employmentType") or "").replace("_", " ").title(),
            "raw_json": {"id": ext, "company_name": job.get("teamDisplayName") or "", "gem_id": raw.get("id")},
        }
