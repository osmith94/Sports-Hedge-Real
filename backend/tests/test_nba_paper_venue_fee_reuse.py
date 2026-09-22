"""NBA PAPER economics reuse existing venue fee snapshots. No NBA fee table."""

from __future__ import annotations

from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import resolve_kalshi_fee_metadata
from sports_hedge.fees.polymarket import resolve_polymarket_fee_metadata


def test_nba_has_no_sport_specific_fee_entry_points() -> None:
    import sports_hedge.fees as fees_pkg
    import sports_hedge.fees.kalshi as kalshi_fees
    import sports_hedge.fees.polymarket as polymarket_fees
    from sports_hedge.fees import resolver

    for module in (fees_pkg, kalshi_fees, polymarket_fees, resolver):
        source = getattr(module, "__file__", "")
        assert source
        text = open(source, encoding="utf-8").read().casefold()
        assert "nba" not in text
        assert "basketball" not in text


def test_kalshi_and_polymarket_fee_resolvers_accept_nba_series_payloads() -> None:
    metadata = resolve_kalshi_fee_metadata(
        series={
            "ticker": "KXNBAGAME",
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": "1",
        }
    )
    assert metadata["series_fee_type"] == "quadratic_with_maker_fees"
    pm = resolve_polymarket_fee_metadata({"feesEnabled": False})
    assert pm is not None


def test_provider_concurrency_constants_unchanged_by_nba() -> None:
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.POLYMARKET] == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
