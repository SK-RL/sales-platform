"""submit-link must resolve already-scanned jobs, not reject them.

Feedback 821bb39d ("Actual job link is saying invalid", HIGH): pasting a
real job link failed with "not a recognized ATS". Root cause — ~45% of
our Greenhouse job URLs are company-domain embeds carrying ``?gh_jid=``
(wiz.io, databricks.com, samsara.com …) and Workable rows are stored
short-form (``apply.workable.com/j/<id>``); the ATS URL parser only
knows ``boards.greenhouse.io`` / long-form hosts, so it 400'd links to
jobs we already had in the DB.

Fix: ``submit_job_link`` now does a DB-first lookup (exact URL, then
``gh_jid`` → the Greenhouse ``external_id``) and returns the existing
row before ever parsing the host. Source-level guard in the suite's
no-live-DB style.
"""

from __future__ import annotations

import inspect
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-submit-link")


def _src() -> str:
    from app.api.v1.jobs import submit_job_link

    return inspect.getsource(submit_job_link)


def test_db_first_lookup_runs_before_parse():
    src = _src()
    # The DB-first resolution must sit BEFORE the parse_job_url call so a
    # recognisable existing job is returned even when the parser would
    # reject its (company-domain) host.
    idx_dbfirst = src.find("Job.url == raw_url")
    idx_parse = src.find("parse_job_url(body.url)")
    assert idx_dbfirst > 0, "submit-link no longer does an exact-URL DB lookup"
    assert idx_parse > 0
    assert idx_dbfirst < idx_parse, (
        "DB-first lookup must precede the ATS parse, else company-domain "
        "Greenhouse (gh_jid) links get 400'd before we check the DB "
        "(feedback 821bb39d)."
    )


def test_gh_jid_is_matched_to_external_id():
    src = _src()
    assert "gh_jid" in src and 'Job.platform == "greenhouse"' in src
    assert "Job.external_id == gh_jid" in src, (
        "Greenhouse external_id is the bare gh_jid — the company-domain "
        "embed URL must resolve to the existing row by that id."
    )


def test_existing_hit_returns_is_new_false():
    src = _src()
    # An already-present job is not a new import.
    assert "is_new=False" in src
