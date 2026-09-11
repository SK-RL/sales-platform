"""F405 — make sure a job has a description before anything is drafted.

Prod, Lexoga (Dover): the "why are you a great fit" draft read like a
résumé dump because the job had NO stored description — Dover's list
endpoint has none, and nothing fetched the detail. The drafter can only
tie an answer to the role when it can see the role. This helper fills
the gap on demand, in order of trust:

1. the stored ``JobDescription`` row;
2. what the stored ``raw_json`` already carries (``extract_description``);
3. the platform fetcher's ``fetch_one`` (detail endpoints: Dover, Gem,
   Jobvite carry the body only there);
4. the posting page itself, text-extracted and trimmed (last resort,
   labelled so the drafter knows it may contain page chrome).

Whatever is found is stored so the next draft, score and search see it.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_MAX_PAGE_TEXT = 8000


def _page_text(url: str) -> str:
    """Visible text of a posting page: main/article if present, else the
    body with nav/header/footer/script removed."""
    try:
        import httpx
        from bs4 import BeautifulSoup

        resp = httpx.get(url, timeout=20.0, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; sales-platform/1.0)"})
        if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "iframe", "svg", "form"]):
            tag.decompose()
        root = soup.find("main") or soup.find("article") or soup.body or soup
        text = re.sub(r"[ \t]+", " ", root.get_text("\n"))
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return text[:_MAX_PAGE_TEXT]
    except Exception as exc:
        logger.info("job_description_service: page fetch failed for %s: %s", url, exc)
        return ""


def ensure_description_sync(session, job, board_slug: str = "") -> tuple[str, str]:
    """Return ``(text, source)``; ``source`` is one of stored / raw_json /
    fetch_one / page / "" (nothing found)."""
    from app.models.job import JobDescription
    from app.utils.job_description import extract_description
    from app.workers.tasks.scan_task import _upsert_job_description
    from sqlalchemy import select

    row = session.execute(select(JobDescription).where(JobDescription.job_id == job.id)).scalar_one_or_none()
    if row is not None and (row.text_content or "").strip():
        return row.text_content, "stored"

    platform = job.platform or ""
    html, text = extract_description(platform, job.raw_json or {})
    source = "raw_json" if (text or html) else ""

    if not (text or html):
        from app.fetchers import FETCHER_MAP

        fetcher_cls = FETCHER_MAP.get(platform)
        slug = board_slug or (job.raw_json or {}).get("company_slug") or ""
        if fetcher_cls is not None and slug and job.external_id:
            try:
                fetcher = fetcher_cls()
                fresh = fetcher.fetch_one(slug, job.external_id)
                if fresh and fresh.get("raw_json"):
                    html, text = extract_description(platform, fresh["raw_json"])
                    if text or html:
                        source = "fetch_one"
                        merged = dict(job.raw_json or {})
                        merged["description"] = fresh["raw_json"].get("description") or html
                        job.raw_json = merged
            except Exception as exc:
                logger.info("job_description_service: fetch_one failed for %s/%s: %s", platform, job.external_id, exc)

    if not (text or html) and job.url:
        text = _page_text(job.url)
        if text:
            source = "page"

    if not text and html:
        from app.utils.job_description import _html_to_text

        text = _html_to_text(html)
    if text or html:
        _upsert_job_description(session, job.id, html or "", text or "")
        session.commit()
        logger.info("job_description_service: description for job %s from %s (%s words)", job.id, source, len((text or "").split()))
    return text or "", source
