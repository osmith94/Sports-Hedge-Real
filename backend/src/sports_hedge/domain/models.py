from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class VenueName(StrEnum):
    MATCHBOOK = "matchbook"
    POLYMARKET = "polymarket"
    SMARKETS = "smarkets"
    KALSHI = "kalshi"


class MarketScope(StrEnum):
    """Canonical economic horizon used to select Min Net Arb.

    FIXTURE_MATCH is a single-match market. COMPETITION_SEASON is a
    competition/season outright. Scope is never inferred from lock duration.
    """

    FIXTURE_MATCH = "FIXTURE_MATCH"
    COMPETITION_SEASON = "COMPETITION_SEASON"


class MarketSide(StrEnum):
    BACK = "back"
    LAY = "lay"
    WIN = "win"
    LOSE = "lose"


class VenueCapabilities(BaseModel):
    data_enabled: bool = True
    paper_enabled: bool = True
    execution_enabled: bool = False


class Event(BaseModel):
    venue: VenueName
    venue_event_id: str
    sport: str
    competition: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    kickoff_utc: datetime | None = None
    status: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class PriceLevel(BaseModel):
    price: Decimal
    available_size: Decimal
    side: MarketSide
    timestamp: datetime | None = None


class Runner(BaseModel):
    venue_runner_id: str
    name: str
    status: str | None = None
    prices: list[PriceLevel] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class Market(BaseModel):
    venue: VenueName
    venue_market_id: str
    venue_event_id: str
    name: str
    market_type: str | None = None
    status: str | None = None
    period: str | None = None
    line: Decimal | None = None
    runners: list[Runner] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class VenueHealth(BaseModel):
    venue: VenueName
    ok: bool
    authenticated: bool
    checked_at: datetime
    detail: str | None = None
