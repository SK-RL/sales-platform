"""F402 — a board whose fetcher raises must still leave a ScanLog row.

Root cause of the 45-day silent Google Sheet outage. The callers
(``scan_all_platforms`` / ``scan_platform``) ``add()`` + ``flush()`` a
``ScanLog`` for the board on the SAME session, then call
``_scan_board``. Pre-fix, ``_scan_board``'s ``except`` did a bare
``session.rollback()`` — which discarded that pending ScanLog insert
along with the board's own partial work. The caller then set
``.errors`` / ``.error_message`` on the now-expunged object and
``commit()``ed a no-op. Net effect: the Celery task returned SUCCESS
with accurate in-memory stats, and nothing ever reached ``scan_logs``.

Both Google Sheet boards had their link-sharing revoked after
2026-07-28 (the CSV export now 401s on Google's login page). Every
one of the ~135 scheduled runs since then hit exactly this path, so
``/monitoring/scan-errors`` showed **zero** google_sheet rows for 45
days while the source was completely dead. The same hole hid any board
on any platform whose fetch throws, not just this one.

The fix scopes the board scan in its own SAVEPOINT
(``session.begin_nested()``), so an exception unwinds only that board's
work and the caller's ScanLog survives in the outer transaction.

These tests use a real in-memory SQLite ``Session`` because the bug is
about transaction semantics — a ``FakeSession`` or a source-text
assertion cannot see it. SQLite supports SAVEPOINT; the two ``event``
listeners are SQLAlchemy's documented pysqlite workaround so that
``begin_nested()`` actually emits one.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f402")


@pytest.fixture
def db():
    from sqlalchemy import JSON, create_engine, event, select
    from sqlalchemy.orm import Session

    from app.database import Base
    from app.models.company import Company, CompanyATSBoard
    from app.models.scan import ScanLog

    # ``Company.tags`` / ``Company.tech_stack`` are Postgres ``ARRAY``
    # columns SQLite cannot create. Swap them to JSON on the shared table
    # metadata for the life of this fixture and restore afterwards so no
    # other test sees the change. ``_scan_board`` never reads either.
    swapped = {}
    for name in ("tags", "tech_stack"):
        col = Company.__table__.c[name]
        swapped[name] = col.type
        col.type = JSON()

    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _no_autobegin(dbapi_conn, _record):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _explicit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(
        engine,
        tables=[Company.__table__, CompanyATSBoard.__table__, ScanLog.__table__],
    )

    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        for name, original in swapped.items():
            Company.__table__.c[name].type = original


def _seed_board(session, platform: str = "google_sheet"):
    from app.models.company import Company, CompanyATSBoard

    company = Company(id=uuid.uuid4(), name="Google Sheet — Team Jobs 1", slug="gsheet-team-1")
    session.add(company)
    session.flush()
    board = CompanyATSBoard(
        id=uuid.uuid4(),
        company_id=company.id,
        platform=platform,
        slug="1nkGCfxuCe3hqWne11G1xpVECNfIUagmUZZynSCJ3Nso",
        is_active=True,
    )
    session.add(board)
    session.commit()
    return board


def _run_like_the_caller(session, board, monkeypatch, fetch_impl):
    """Mirror ``scan_platform``'s per-board sequence exactly.

    The order matters: the ScanLog is added + flushed BEFORE
    ``_scan_board`` runs, on the same session. That is the invariant the
    bug violated.
    """
    from app.models.scan import ScanLog
    from app.workers.tasks import scan_task

    class _Fetcher:
        def fetch(self, slug):
            return fetch_impl(slug)

    monkeypatch.setattr(scan_task, "_get_fetcher_for_platform", lambda _p: _Fetcher())

    scan_log = ScanLog(
        id=uuid.uuid4(),
        source=f"{board.platform}/{board.slug}",
        platform=board.platform,
    )
    session.add(scan_log)
    session.flush()

    stats = scan_task._scan_board(session, board, cluster_config=None)

    scan_log.jobs_found = stats["jobs_found"]
    scan_log.errors = stats["errors"]
    scan_log.error_message = stats["error_message"]
    session.commit()
    return stats


def test_failed_fetch_still_persists_the_scanlog_row(db, monkeypatch):
    """The regression. A raising fetcher must leave a row with errors > 0."""
    from sqlalchemy import select

    from app.models.scan import ScanLog

    board = _seed_board(db)

    def _raise(_slug):
        raise ValueError("Google Sheet 1nkGCfxu is not link-shared. Open the sheet …")

    stats = _run_like_the_caller(db, board, monkeypatch, _raise)

    assert stats["errors"] == 1
    assert "not link-shared" in stats["error_message"]

    # Fresh query, not the in-memory object: what actually reached the table.
    rows = db.execute(select(ScanLog).where(ScanLog.platform == "google_sheet")).scalars().all()
    assert len(rows) == 1, (
        "the ScanLog the caller flushed before _scan_board was discarded — "
        "_scan_board rolled back the OUTER transaction instead of its own savepoint"
    )
    assert rows[0].errors == 1
    assert "not link-shared" in rows[0].error_message


def test_failed_fetch_does_not_stamp_last_scanned_at(db, monkeypatch):
    """A failed scan is not a scan. Staleness must stay visible."""
    from sqlalchemy import select

    from app.models.company import CompanyATSBoard

    board = _seed_board(db)

    def _raise(_slug):
        raise RuntimeError("HTTP 401")

    _run_like_the_caller(db, board, monkeypatch, _raise)

    db.expire_all()
    fresh = db.execute(select(CompanyATSBoard).where(CompanyATSBoard.id == board.id)).scalar_one()
    assert fresh.last_scanned_at is None


def test_successful_empty_fetch_still_commits_normally(db, monkeypatch):
    """Guard the happy path: the SAVEPOINT wrapper must not break success."""
    from sqlalchemy import select

    from app.models.company import CompanyATSBoard
    from app.models.scan import ScanLog

    board = _seed_board(db)

    stats = _run_like_the_caller(db, board, monkeypatch, lambda _slug: [])

    assert stats["errors"] == 0
    assert stats["jobs_found"] == 0

    rows = db.execute(select(ScanLog).where(ScanLog.platform == "google_sheet")).scalars().all()
    assert len(rows) == 1
    assert rows[0].errors == 0

    db.expire_all()
    fresh = db.execute(select(CompanyATSBoard).where(CompanyATSBoard.id == board.id)).scalar_one()
    assert fresh.last_scanned_at is not None, "a clean scan must stamp last_scanned_at"


def test_scan_board_no_longer_rolls_back_the_whole_session():
    """Belt-and-braces source guard, same style as the F320/F284 tests.

    If someone reintroduces a bare ``session.rollback()`` in
    ``_scan_board``'s except block, the SAVEPOINT above becomes
    pointless and the behavioural tests will fail too — this just names
    the exact line to look at.
    """
    import inspect

    from app.workers.tasks import scan_task

    src = inspect.getsource(scan_task._scan_board)
    tail = src[src.rfind("except Exception as e:"):]
    # Only code lines: the fix's own comment names the call it removed.
    code_only = "\n".join(
        line for line in tail.splitlines() if not line.lstrip().startswith("#")
    )
    assert "session.rollback()" not in code_only, (
        "_scan_board's except block must not call session.rollback() — "
        "it discards the caller's flushed ScanLog (F402)"
    )
    assert "session.begin_nested()" in src, "_scan_board must scope the board scan in a SAVEPOINT (F402)"
