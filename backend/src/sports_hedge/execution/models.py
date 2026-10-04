"""Live-execution records for one already-approved hedge package.

These models carry an order to a venue execution client and the acknowledgement
that comes back. They do not restate fees, FX, freshness, depth, or Treasury.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.domain.models import MarketSide, VenueName


class VenueOrderStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    OPEN = "open"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class LivePackageOutcome(StrEnum):
    FULLY_FILLED = "FULLY_FILLED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class VenueOrderRequest(BaseModel):
    """One hedge leg prepared from an accepted Price-2 plan."""

    venue: VenueName
    trade_id: str
    tranche_id: str
    native_event_id: str
    native_market_id: str
    native_runner_id: str
    side: MarketSide
    currency: str = "GBP"
    requested_price: Decimal = Field(gt=0)
    requested_size: Decimal = Field(gt=0)
    client_order_id: str


class VenueOrderResult(BaseModel):
    """Venue acknowledgement for one client order. Sizes stay in the request's units."""

    venue: VenueName
    client_order_id: str
    venue_order_id: str | None = None
    status: VenueOrderStatus
    requested_size: Decimal
    # None means this response did not supply a cumulative fill. It is not zero.
    filled_size: Decimal | None = Field(default=None, ge=0)
    requested_price: Decimal
    average_fill_price: Decimal | None = None
    submitted_at: datetime
    updated_at: datetime
    # Venue-native quantities. Matchbook stake is account currency. Kalshi
    # quantity is contracts. ``filled_size`` stays in the request's currency units.
    native_filled_quantity: Decimal | None = None
    native_remaining_quantity: Decimal | None = None
    order_type: str | None = None
    venue_fee: Decimal | None = None
    venue_fee_rate_bps: Decimal | None = None
    remainder_quantity: Decimal | None = None
    cancel_result: str | None = None
    eligibility: dict[str, Any] | None = None
    note: str | None = None


class LiveExecutionPackage(BaseModel):
    """Package outcome plus each leg. ``detail`` explains a refusal before dispatch."""

    outcome: LivePackageOutcome
    orders: list[VenueOrderResult] = Field(default_factory=list)
    detail: str | None = None
