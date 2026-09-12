from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.paper.fills import (
    FillMode,
    PaperFillConfig,
    PaperOpportunityFills,
    PaperOpportunityLeg,
)
from sports_hedge.paper.simulator import (
    INSUFFICIENT_DEPTH,
    NO_VISIBLE_DEPTH,
    PaperFillSimulator,
    STALE_QUOTE,
    UNKNOWN_QUOTE_AGE,
)


CAPTURED = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _leg(
    *,
    requested: str,
    displayed: str,
    levels: list[BookLevel],
    quote_age_ms: int = 0,
    outcome: str = "yes",
    venue: VenueName = VenueName.MATCHBOOK,
    currency: str = "GBP",
) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id="mb-btts",
        source_runner_id="yes-1",
        currency=currency,
        requested_stake=Decimal(requested),
        displayed_odds=Decimal(displayed),
        levels=levels,
        quote_age_ms=quote_age_ms,
        quote_captured_at=CAPTURED,
    )


def _two_level_book() -> list[BookLevel]:
    return [
        BookLevel(decimal_odds=Decimal("2.20"), available_stake=Decimal("40")),
        BookLevel(decimal_odds=Decimal("2.10"), available_stake=Decimal("80")),
    ]


def test_ideal_mode_fully_fills_at_displayed_price_ignoring_thin_depth() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate(
        [
            _leg(
                requested="100",
                displayed="2.20",
                levels=_two_level_book(),
            )
        ],
        PaperFillConfig(mode=FillMode.IDEAL),
        opportunity_id="opp-ideal",
        now=CAPTURED,
    )

    fill = result.fills[0]
    assert fill.mode is FillMode.IDEAL
    assert fill.fully_filled is True
    assert fill.filled_stake == Decimal("100")
    assert fill.remaining_stake == Decimal("0")
    assert fill.weighted_odds == Decimal("2.20")
    assert fill.slippage_bps == Decimal("0")
    assert fill.levels_consumed == 0
    assert fill.rejection_reason is None
    assert fill.venue is VenueName.MATCHBOOK
    assert fill.currency == "GBP"
    assert fill.source_market_id == "mb-btts"
    assert fill.outcome == "yes"
    assert fill.theoretical_payout == Decimal("220.00")
    assert fill.realised_payout == Decimal("220.00")
    assert result.fully_filled is True


def test_realistic_full_fill_at_displayed_top_of_book() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate_leg(
        _leg(
            requested="40",
            displayed="2.20",
            levels=_two_level_book(),
        ),
        PaperFillConfig(mode=FillMode.REALISTIC, max_quote_age_ms=1000),
        now=CAPTURED,
    )

    assert result.fully_filled is True
    assert result.filled_stake == Decimal("40")
    assert result.weighted_odds == Decimal("2.20")
    assert result.worst_odds == Decimal("2.20")
    assert result.levels_consumed == 1
    assert result.slippage_bps == Decimal("0")
    assert result.rejection_reason is None


def test_multi_level_weighted_fill() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate_leg(
        _leg(
            requested="80",
            displayed="2.20",
            levels=_two_level_book(),
        ),
        PaperFillConfig(mode=FillMode.REALISTIC),
        now=CAPTURED,
    )

    assert result.fully_filled is True
    assert result.filled_stake == Decimal("80")
    assert result.levels_consumed == 2
    assert result.worst_odds == Decimal("2.10")
    assert result.weighted_odds == Decimal("2.15")
    assert result.realised_payout == Decimal("172.00")
    assert result.theoretical_payout == Decimal("176.00")
    assert result.slippage_bps > 0
    assert result.rejection_reason is None


def test_insufficient_depth_creates_partial_unfilled_residual() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate_leg(
        _leg(
            requested="150",
            displayed="2.20",
            levels=_two_level_book(),
        ),
        PaperFillConfig(mode=FillMode.REALISTIC),
        now=CAPTURED,
    )

    assert result.fully_filled is False
    assert result.filled_stake == Decimal("120")
    assert result.remaining_stake == Decimal("30")
    assert result.levels_consumed == 2
    assert result.weighted_odds == Decimal("2.133333333333333333333333333")
    assert result.rejection_reason == INSUFFICIENT_DEPTH
    assert result.realised_payout == result.filled_stake * result.weighted_odds


def test_empty_book_is_unfilled_not_a_fictitious_full_fill() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate_leg(
        _leg(requested="50", displayed="2.20", levels=[]),
        PaperFillConfig(mode=FillMode.REALISTIC),
        now=CAPTURED,
    )

    assert result.filled_stake == Decimal("0")
    assert result.remaining_stake == Decimal("50")
    assert result.weighted_odds is None
    assert result.rejection_reason == NO_VISIBLE_DEPTH
    assert result.realised_payout == Decimal("0")


def test_stale_quote_is_rejected_after_latency() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate_leg(
        _leg(
            requested="40",
            displayed="2.20",
            levels=_two_level_book(),
            quote_age_ms=800,
        ),
        PaperFillConfig(
            mode=FillMode.REALISTIC,
            assumed_latency_ms=500,
            max_quote_age_ms=1000,
        ),
        now=CAPTURED,
    )

    assert result.filled_stake == Decimal("0")
    assert result.remaining_stake == Decimal("40")
    assert result.weighted_odds is None
    assert result.rejection_reason == STALE_QUOTE
    assert result.assumed_latency_ms == 500
    assert result.quote_age_ms == 800
    assert result.filled_at == datetime(2026, 9, 11, 12, 0, 0, 500000, tzinfo=UTC)


def test_worse_latency_slippage_and_depth_worsen_realised_economics() -> None:
    simulator = PaperFillSimulator()
    leg = _leg(requested="80", displayed="2.20", levels=_two_level_book(), quote_age_ms=100)

    baseline = simulator.simulate(
        [leg],
        PaperFillConfig(mode=FillMode.REALISTIC, assumed_latency_ms=0, slippage_bps=Decimal("0")),
        opportunity_id="opp-base",
        now=CAPTURED,
    )
    slipped = simulator.simulate(
        [leg],
        PaperFillConfig(
            mode=FillMode.REALISTIC,
            assumed_latency_ms=0,
            slippage_bps=Decimal("25"),
        ),
        opportunity_id="opp-slip",
        now=CAPTURED,
    )
    delayed = simulator.simulate(
        [leg],
        PaperFillConfig(
            mode=FillMode.REALISTIC,
            assumed_latency_ms=250,
            ms_per_skipped_level=250,
            max_quote_age_ms=2000,
        ),
        opportunity_id="opp-latency",
        now=CAPTURED,
    )
    thinner = simulator.simulate(
        [
            _leg(
                requested="80",
                displayed="2.20",
                levels=[BookLevel(decimal_odds=Decimal("2.20"), available_stake=Decimal("40"))],
                quote_age_ms=100,
            )
        ],
        PaperFillConfig(mode=FillMode.REALISTIC),
        opportunity_id="opp-thin",
        now=CAPTURED,
    )
    ideal = simulator.simulate(
        [leg],
        PaperFillConfig(mode=FillMode.IDEAL),
        opportunity_id="opp-ideal",
        now=CAPTURED,
    )

    assert ideal.fills[0].realised_payout > baseline.fills[0].realised_payout
    assert baseline.fills[0].realised_payout > slipped.fills[0].realised_payout
    assert slipped.fills[0].weighted_odds == Decimal("2.15") * Decimal("0.9975")
    assert delayed.fills[0].levels_consumed == 1
    assert delayed.fills[0].weighted_odds == Decimal("2.10")
    assert delayed.fills[0].realised_payout < baseline.fills[0].realised_payout
    assert thinner.fills[0].filled_stake == Decimal("40")
    assert thinner.fills[0].remaining_stake == Decimal("40")
    assert thinner.fills[0].realised_payout < baseline.fills[0].realised_payout
    assert thinner.fully_filled is False
    assert thinner.rejection_reasons == [INSUFFICIENT_DEPTH]


def test_opportunity_preserves_cross_venue_currency_and_ids() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate(
        [
            PaperOpportunityLeg(
                outcome="yes",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb-2001",
                source_runner_id="301",
                currency="GBP",
                requested_stake=Decimal("40"),
                displayed_odds=Decimal("2.20"),
                levels=[BookLevel(decimal_odds=Decimal("2.20"), available_stake=Decimal("40"))],
                quote_age_ms=100,
            ),
            PaperOpportunityLeg(
                outcome="no",
                venue=VenueName.POLYMARKET,
                source_market_id="pm-market-1",
                source_runner_id="no-token",
                currency="USD",
                requested_stake=Decimal("40"),
                displayed_odds=Decimal("2.10"),
                levels=[BookLevel(decimal_odds=Decimal("2.10"), available_stake=Decimal("200"))],
                quote_age_ms=150,
            ),
        ],
        PaperFillConfig(mode=FillMode.REALISTIC),
        opportunity_id="opp-cross",
        now=CAPTURED,
    )

    by_venue = {fill.venue: fill for fill in result.fills}
    assert by_venue[VenueName.MATCHBOOK].currency == "GBP"
    assert by_venue[VenueName.MATCHBOOK].source_market_id == "mb-2001"
    assert by_venue[VenueName.POLYMARKET].currency == "USD"
    assert by_venue[VenueName.POLYMARKET].source_runner_id == "no-token"
    assert all(fill.fully_filled for fill in result.fills)
    assert result.mode is FillMode.REALISTIC
    assert result.fully_filled is True
    totals = {(item.currency, item.venue): item for item in result.native_stake_totals()}
    assert totals[("GBP", VenueName.MATCHBOOK)].requested_stake == Decimal("40")
    assert totals[("GBP", VenueName.MATCHBOOK)].filled_stake == Decimal("40")
    assert totals[("USD", VenueName.POLYMARKET)].requested_stake == Decimal("40")
    assert totals[("USD", VenueName.POLYMARKET)].filled_stake == Decimal("40")
    assert not hasattr(PaperOpportunityFills, "requested_stake")
    assert not hasattr(PaperOpportunityFills, "filled_stake")
    assert not hasattr(PaperOpportunityFills, "remaining_stake")
    assert not hasattr(PaperOpportunityFills, "theoretical_payout")
    assert not hasattr(PaperOpportunityFills, "realised_payout")
    assert not hasattr(PaperOpportunityFills, "theoretical_profit")
    assert not hasattr(PaperOpportunityFills, "realised_profit")


def test_fill_time_is_simulation_clock_plus_latency_not_quote_capture() -> None:
    captured = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    simulated_at = captured + timedelta(seconds=2)
    simulator = PaperFillSimulator()
    result = simulator.simulate_leg(
        PaperOpportunityLeg(
            outcome="yes",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-btts",
            source_runner_id="yes-1",
            requested_stake=Decimal("40"),
            displayed_odds=Decimal("2.20"),
            levels=_two_level_book(),
            quote_age_ms=2000,
            quote_captured_at=captured,
        ),
        PaperFillConfig(
            mode=FillMode.REALISTIC,
            assumed_latency_ms=500,
            max_quote_age_ms=3000,
        ),
        now=simulated_at,
    )

    assert result.quote_captured_at == captured
    assert result.filled_at == simulated_at + timedelta(milliseconds=500)
    assert result.filled_at > simulated_at
    assert result.filled_at != captured + timedelta(milliseconds=500)


def test_unknown_quote_age_fails_closed_instead_of_assuming_fresh() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate_leg(
        PaperOpportunityLeg(
            outcome="yes",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-btts",
            source_runner_id="yes-1",
            requested_stake=Decimal("40"),
            displayed_odds=Decimal("2.20"),
            levels=_two_level_book(),
            quote_age_ms=None,
        ),
        PaperFillConfig(mode=FillMode.REALISTIC, max_quote_age_ms=1000),
        now=CAPTURED,
    )

    assert result.filled_stake == Decimal("0")
    assert result.remaining_stake == Decimal("40")
    assert result.rejection_reason == UNKNOWN_QUOTE_AGE
    assert result.quote_age_ms is None


def test_quote_age_at_freshness_cap_is_stale() -> None:
    simulator = PaperFillSimulator()
    result = simulator.simulate_leg(
        _leg(
            requested="40",
            displayed="2.20",
            levels=_two_level_book(),
            quote_age_ms=1000,
        ),
        PaperFillConfig(mode=FillMode.REALISTIC, assumed_latency_ms=0, max_quote_age_ms=1000),
        now=CAPTURED,
    )

    assert result.filled_stake == Decimal("0")
    assert result.rejection_reason == STALE_QUOTE
