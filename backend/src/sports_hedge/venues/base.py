from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from sports_hedge.domain.models import VenueCapabilities, VenueHealth, VenueName


def market_data_http_timeout() -> httpx.Timeout:
    """Bounded timeouts for Phase 1 read-only venue calls.

    Connect/read splits keep a hanging Windows TLS or proxy handshake from
    blocking collection after the documented per-request budget.
    """

    return httpx.Timeout(connect=5.0, read=8.0, write=8.0, pool=5.0)


class ReadOnlyVenue(ABC):
    """Market-data-only venue contract used by Phase 1.

    There is intentionally no order placement or cancellation interface here.
    """

    name: VenueName
    capabilities = VenueCapabilities(
        data_enabled=True,
        paper_enabled=True,
        execution_enabled=False,
    )

    @abstractmethod
    async def list_events(self, **filters: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def list_markets(self, event_id: int | str, **filters: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """Return the deepest available read-only order-book representation."""
        raise NotImplementedError

    @abstractmethod
    async def health(self) -> VenueHealth:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any underlying network resources."""
