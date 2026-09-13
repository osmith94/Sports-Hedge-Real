from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sports_hedge.domain.models import VenueName
from sports_hedge.paper.liquidity import PaperLiquidityPool, PaperLiquiditySnapshot


NOW = datetime(2026, 9, 13, 8, 45, tzinfo=UTC)


def _pool(venue: VenueName) -> PaperLiquidityPool:
    if venue is VenueName.MATCHBOOK:
        return PaperLiquidityPool(
            venue=venue,
            native_currency="GBP",
            available=Decimal("1000"),
            included_in_solver=True,
            connection_status="connected",
        )
    if venue is VenueName.POLYMARKET:
        return PaperLiquidityPool(
            venue=venue,
            native_currency="USD",
            available=Decimal("1200"),
            included_in_solver=True,
            connection_status="connected",
        )
    if venue is VenueName.SMARKETS:
        return PaperLiquidityPool(
            venue=venue,
            native_currency="GBP",
            available=Decimal("0"),
            included_in_solver=False,
            connection_status="not_connected",
        )
    raise AssertionError("helper only covers legacy pre-Kalshi pools")


def test_pre_k1_snapshot_is_upgraded_with_zero_funded_separate_kalshi_pool() -> None:
    snapshot = PaperLiquiditySnapshot(
        updated_at=NOW,
        pools=[
            _pool(VenueName.MATCHBOOK),
            _pool(VenueName.POLYMARKET),
            _pool(VenueName.SMARKETS),
        ],
    )

    kalshi = snapshot.pool(VenueName.KALSHI)
    assert kalshi.native_currency == "USD"
    assert kalshi.available == Decimal("0")
    assert kalshi.locked == Decimal("0")
    assert kalshi.transit == Decimal("0")
    assert kalshi.included_in_solver is True
    assert kalshi.connection_status == "connected"
    assert snapshot.pool(VenueName.POLYMARKET).available == Decimal("1200")


def test_snapshot_missing_non_kalshi_required_pool_still_fails_closed() -> None:
    with pytest.raises(ValidationError, match="missing standing pools"):
        PaperLiquiditySnapshot(
            updated_at=NOW,
            pools=[
                _pool(VenueName.MATCHBOOK),
                _pool(VenueName.SMARKETS),
            ],
        )
