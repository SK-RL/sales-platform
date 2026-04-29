"""Pydantic schemas for Discovery endpoints."""

from typing import Literal
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from uuid import UUID


class DiscoveryRunOut(BaseModel):
    id: UUID
    source: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    companies_found: int
    new_companies: int

    model_config = {"from_attributes": True}


class DiscoveredCompanyOut(BaseModel):
    id: UUID
    discovery_run_id: UUID
    name: str
    platform: str
    slug: str
    careers_url: str
    status: str
    relevance_hint: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DiscoveredCompanyUpdate(BaseModel):
    # F306: ``extra="forbid"`` to catch admin-side typos like
    # ``stauts`` instead of silently ignoring them.
    # Status is also Literal-typed — the handler at
    # ``api/v1/discovery.py::update_discovered_status`` validates
    # against ``("added", "ignored")`` at runtime; lifting that
    # constraint to the schema means non-canonical values 422 at
    # parse time before reaching the DB.
    model_config = ConfigDict(extra="forbid")

    status: Literal["added", "ignored"]
