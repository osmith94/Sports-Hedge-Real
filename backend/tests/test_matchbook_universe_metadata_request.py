"""UNIVERSE Matchbook listings are metadata requests.

`list_markets` still defaults to priced ladders for diagnostic and HOT
refresh callers. ScanLane.UNIVERSE must not ask for those prices.
`get_market` stays priced for BACKGROUND / HOT / ACTIVE.

Fixture/demo HTTP capture. Not live Matchbook traffic.
"""

from __future__ import annotations

from typing import Any

import pytest

from sports_hedge.config import Settings
from sports_hedge.venues.matchbook import MatchbookClient


@pytest.mark.asyncio
async def test_metadata_listing_omits_prices_and_get_market_keeps_them() -> None:
    settings = Settings()
    venue = MatchbookClient(settings)
    captured: list[dict[str, Any]] = []

    async def _get(path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        del path
        captured.append(dict(params))
        return {
            "markets": [{"id": 9, "name": "Match Odds", "runners": []}],
            "total": 1,
            "offset": 0,
            "per-page": 20,
            "id": 9,
        }

    venue._get = _get  # type: ignore[method-assign]
    try:
        await venue.list_markets(44, **{"include-prices": "false", "price-depth": 5})
        metadata = captured[-1]
        assert metadata["include-prices"] == "false"
        assert "price-depth" not in metadata

        await venue.list_markets(44)
        priced_list = captured[-1]
        assert priced_list["include-prices"] == "true"
        assert priced_list["price-depth"] == settings.matchbook_price_depth

        await venue.get_market(44, 9)
        priced_market = captured[-1]
        assert priced_market["include-prices"] == "true"
        assert priced_market["price-depth"] == settings.matchbook_price_depth
    finally:
        await venue.aclose()
