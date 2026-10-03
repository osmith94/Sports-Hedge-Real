"""Venue execution clients, separate from the read-only market-data clients.

Callers inject the transport. The deterministic transport never performs
venue I/O. Authenticated Matchbook and Kalshi HTTP transports live in their
own modules and are not constructed by the scanner.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sports_hedge.domain.models import VenueName
from sports_hedge.execution.models import VenueOrderRequest, VenueOrderResult, VenueOrderStatus


class ExecutionTransport(Protocol):
    """Order transport injected into an execution client."""

    async def dispatch(self, request: VenueOrderRequest) -> VenueOrderResult:
        """Return one order acknowledgement. HTTP transports may perform venue I/O."""


class DeterministicExecutionTransport:
    """Scripted fill for tests that inject this transport explicitly.

    Runtime execution does not construct or select this type. ``filled_fraction``
    is applied to the requested size. ``1`` fills the leg, a fraction between 0
    and 1 is partial, and ``0`` or ``fail`` returns no fill. The echoed average
    price is the requested price. This is not venue slippage.
    """

    test_only = True
    transport_kind = "deterministic_test"

    def __init__(
        self,
        *,
        filled_fraction: Decimal = Decimal(1),
        fail: bool = False,
        delay_seconds: float = 0,
    ) -> None:
        if filled_fraction < 0:
            raise ValueError("filled_fraction must be non-negative")
        self.filled_fraction = filled_fraction
        self.fail = fail
        self.delay_seconds = delay_seconds
        self.calls: list[VenueOrderRequest] = []

    async def dispatch(self, request: VenueOrderRequest) -> VenueOrderResult:
        self.calls.append(request)
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        now = datetime.now(UTC)
        if self.fail or self.filled_fraction <= 0:
            return VenueOrderResult(
                venue=request.venue,
                client_order_id=request.client_order_id,
                venue_order_id=None,
                status=VenueOrderStatus.FAILED if self.fail else VenueOrderStatus.REJECTED,
                requested_size=request.requested_size,
                filled_size=Decimal(0),
                requested_price=request.requested_price,
                average_fill_price=None,
                submitted_at=now,
                updated_at=now,
            )
        filled = min(request.requested_size * self.filled_fraction, request.requested_size)
        if filled == request.requested_size:
            status = VenueOrderStatus.FILLED
        else:
            status = VenueOrderStatus.PARTIAL
        return VenueOrderResult(
            venue=request.venue,
            client_order_id=request.client_order_id,
            venue_order_id=f"mock:{request.client_order_id}",
            status=status,
            requested_size=request.requested_size,
            filled_size=filled,
            requested_price=request.requested_price,
            average_fill_price=request.requested_price,
            submitted_at=now,
            updated_at=now,
        )


class MatchbookExecutionClient:
    """Matchbook order dispatch. Not a market-data client and not a venue HTTP call."""

    venue = VenueName.MATCHBOOK

    def __init__(self, transport: ExecutionTransport) -> None:
        self._transport = transport

    async def dispatch(self, request: VenueOrderRequest) -> VenueOrderResult:
        if request.venue is not VenueName.MATCHBOOK:
            raise ValueError("Matchbook execution client received another venue")
        return await self._transport.dispatch(request)

    async def cancel(self, request: VenueOrderRequest) -> VenueOrderResult:
        """Cancel via the injected transport. No cancel method does not submit again."""

        if request.venue is not VenueName.MATCHBOOK:
            raise ValueError("Matchbook execution client received another venue")
        cancel = getattr(self._transport, "cancel", None)
        if cancel is None:
            now = datetime.now(UTC)
            return VenueOrderResult(
                venue=request.venue,
                client_order_id=request.client_order_id,
                venue_order_id=None,
                status=VenueOrderStatus.FAILED,
                requested_size=request.requested_size,
                filled_size=None,
                requested_price=request.requested_price,
                average_fill_price=None,
                submitted_at=now,
                updated_at=now,
            )
        return await cancel(request)


class KalshiExecutionClient:
    """Kalshi order dispatch. Not a market-data client and not a venue HTTP call."""

    venue = VenueName.KALSHI

    def __init__(self, transport: ExecutionTransport) -> None:
        self._transport = transport

    async def dispatch(self, request: VenueOrderRequest) -> VenueOrderResult:
        if request.venue is not VenueName.KALSHI:
            raise ValueError("Kalshi execution client received another venue")
        return await self._transport.dispatch(request)
