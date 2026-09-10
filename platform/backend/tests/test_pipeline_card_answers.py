"""The pipeline card's drill-down must carry the answers we submitted.

Clicking a card on the Pipeline board opens a side panel listing every
application under that company. The operator's question there is "what
did we actually tell this employer?" — which previously meant leaving the
board, finding the job, and opening Job Detail. The answers already exist
on ``Application.prepared_answers`` (written by ``/applications/record``
when the browser-side lane fills a form); this endpoint just never
returned them.

Source-level, no DB — same style as the other application tests.
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
os.environ.setdefault("JWT_SECRET", "pytest-pipeline-answers")


def test_card_drilldown_route_registered():
    from tests._routes import registered_routes

    assert ("GET", "/api/v1/pipeline/{client_id}/applications") in registered_routes()


def test_drilldown_returns_prepared_answers():
    from app.api.v1.pipeline import list_client_applications

    src = inspect.getsource(list_client_applications)
    assert '"prepared_answers": app.prepared_answers or []' in src, (
        "card side panel cannot show what was submitted without the answers"
    )


def test_drilldown_returns_answer_count_and_apply_method():
    """The panel needs a count to render a summary without walking the list,
    and apply_method to badge rows filed by the browser-side lane."""
    from app.api.v1.pipeline import list_client_applications

    src = inspect.getsource(list_client_applications)
    assert '"answer_count": len(app.prepared_answers or [])' in src
    assert '"apply_method": app.apply_method' in src


def test_prepared_answers_defaults_to_list_not_none():
    """``Application.prepared_answers`` is JSON-nullable in practice; the
    panel must never receive null where it iterates."""
    from app.api.v1.pipeline import list_client_applications

    src = inspect.getsource(list_client_applications)
    assert "app.prepared_answers or []" in src
    assert '"prepared_answers": app.prepared_answers,' not in src


def test_drilldown_is_scoped_to_the_cards_company():
    """A card must never leak another company's applications."""
    from app.api.v1.pipeline import list_client_applications

    src = inspect.getsource(list_client_applications)
    assert "Application.company_id == client.company_id" in src
