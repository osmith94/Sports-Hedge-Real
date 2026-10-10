"""Issue #39: native orders use the worst admitted BACK/BUY limit, not best odds.

Fixture books and fake transports only. Execution stays disarmed. No live orders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_hedge.application.active_trade_recovery import remaining_book_levels
from sports_hedge.application.paper_scan import _fill_legs_from_observations
from sports_hedge.arbitrage.depth import (
    DepthAwareCompleteSetScanner,
    DepthQuoteSource,
    prefix_depth_candidates,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.clients import (
    DeterministicExecutionTransport,
    MatchbookExecutionClient,
    PolymarketExecutionClient,
)
from sports_hedge.execution.depth_plan import (
    approved_back_limit_odds,
    conservative_complete_set_solution,
    exact_frozen_settlement,
    qualifies_at_conservative_limits,
    stamp_fill_leg_depth_plan,
    sweep_back_levels,
)
from sports_hedge.execution.dispatch import (
    LiveDispatchResult,
    refusal_reason,
    release_matchbook_remainders,
    run_live_opening,
)
from sports_hedge.execution.frozen import freeze_native_orders
from sports_hedge.execution.models import VenueOrderResult, VenueOrderStatus
from sports_hedge.execution.package import execution_armed
from sports_hedge.execution.translate import POLYMARKET_ORDER_TYPE as PM_FAK
from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
)
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.paper.fills import FillMode, PaperFillConfig, PaperOpportunityLeg
from sports_hedge.paper.simulator import PaperFillSimulator
from sports_hedge.venues.matchbook import MatchbookClient

NOW = datetime(2026, 10, 10, 16, tzinfo=UTC)
TOKEN = "8" * 76
GBP_PER_USD = Decimal("0.80")


def _level(odds: str, stake: str) -> BookLevel:
    return BookLevel(decimal_odds=Decimal(odds), available_stake=Decimal(stake))


def _cost(venue: VenueName, *, action: MarketAction, rate: str | None = None) -> VenueCostSnapshot:
    if rate is None:
        return VenueCostSnapshot(
            venue=venue,
            action=action,
            fee_basis=FeeBasis.NONE_CONFIRMED,
            known_status=CostKnownStatus.KNOWN,
            captured_at=NOW,
            source="issue39-fixture",
            order_role=OrderRole.TAKER,
            fee_scope=FeeScope.PER_QUOTE,
            currency="GBP" if venue is VenueName.MATCHBOOK else "USD",
        )
    return VenueCostSnapshot.per_quote_profit_commission(
        venue,
        Decimal(rate),
        action=action,
        source="issue39-fixture",
        captured_at=NOW,
        currency="GBP" if venue is VenueName.MATCHBOOK else "USD",
        order_role=OrderRole.TAKER,
    )


def _mb_leg(*, stake: str, levels: list[BookLevel] | None = None) -> PaperOpportunityLeg:
    book = levels or [_level("2.20", "0.50"), _level("2.00", "100")]
    return stamp_fill_leg_depth_plan(
        PaperOpportunityLeg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-mkt",
            source_runner_id="101",
            source_event_id="evt-mb",
            currency="GBP",
            requested_stake=Decimal(stake),
            displayed_odds=Decimal("2.20"),
            levels=book,
        )
    )


def _pm_leg(*, stake: str, levels: list[BookLevel] | None = None) -> PaperOpportunityLeg:
    book = levels or [_level("2.50", "100")]
    return stamp_fill_leg_depth_plan(
        PaperOpportunityLeg(
            outcome="away",
            venue=VenueName.POLYMARKET,
            source_market_id="condition-1",
            source_runner_id=TOKEN,
            source_event_id="evt-pm",
            currency="USD",
            requested_stake=Decimal(stake),
            displayed_odds=book[0].decimal_odds,
            levels=book,
        )
    )


def _pm_books(*, minimum: str = "5") -> dict[str, dict[str, str]]:
    return {TOKEN: {"tick_size": "0.01", "min_order_size": minimum}}


def test_default_execution_stays_disarmed() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert execution_armed(settings) is False


def test_universe_filter_would_drop_fifty_pence_but_price2_does_not() -> None:
    settings = Settings()
    assert settings.matchbook_minimum_liquidity == 2.0
    assert settings.matchbook_price_depth == 5
    assert settings.matchbook_price2_minimum_liquidity == 0.0
    assert settings.matchbook_price2_price_depth == 20
    listing = MatchbookClient(settings)._market_data_params()
    exact = MatchbookClient(settings)._exact_id_price_params()
    assert listing["minimum-liquidity"] == 2.0
    assert Decimal("0.50") < Decimal(str(listing["minimum-liquidity"]))
    assert exact["minimum-liquidity"] == 0.0
    assert exact["price-depth"] == 20


@pytest.mark.asyncio
async def test_get_market_uses_price2_depth_not_universe_listing_params() -> None:
    settings = Settings()
    venue = MatchbookClient(settings)
    captured: list[dict] = []

    async def _get(path: str, *, params: dict) -> dict:
        del path
        captured.append(dict(params))
        return {"id": 9, "markets": []}

    venue._get = _get  # type: ignore[method-assign]
    try:
        await venue.list_markets(44)
        listing = captured[-1]
        await venue.get_market(44, 9)
        exact = captured[-1]
    finally:
        await venue.aclose()
    assert listing["minimum-liquidity"] == settings.matchbook_minimum_liquidity
    assert listing["price-depth"] == settings.matchbook_price_depth
    assert exact["minimum-liquidity"] == settings.matchbook_price2_minimum_liquidity
    assert exact["price-depth"] == settings.matchbook_price2_price_depth


def test_five_pound_sweep_uses_second_level_worst_limit_not_top_of_book() -> None:
    plan = sweep_back_levels(
        [_level("2.20", "0.50"), _level("2.00", "100")],
        Decimal("5.00"),
    )
    assert plan.fully_filled is True
    assert plan.filled_stake == Decimal("5.00")
    assert plan.levels[0].stake == Decimal("0.50")
    assert plan.levels[1].stake == Decimal("4.50")
    assert plan.worst_limit_odds == Decimal("2.00")
    assert plan.expected_average_odds == Decimal("2.02")
    assert plan.worst_limit_odds != Decimal("2.20")

    mb = _mb_leg(stake="5.00")
    pm = _pm_leg(stake="6.25")
    assert mb.displayed_odds == Decimal("2.20")
    assert mb.approved_depth_worst_limit == Decimal("2.00")
    assert mb.expected_average_odds == Decimal("2.02")
    assert approved_back_limit_odds(mb) == Decimal("2.00")

    costs = {
        VenueName.MATCHBOOK: _cost(VenueName.MATCHBOOK, action=MarketAction.BACK),
        VenueName.POLYMARKET: _cost(VenueName.POLYMARKET, action=MarketAction.BUY),
    }
    fx = {"GBP": Decimal(1), "USD": GBP_PER_USD}
    matched = [mb, _pm_leg(stake="5.00")]
    assert qualifies_at_conservative_limits(
        matched,
        costs=costs,
        gbp_per_unit=fx,
        minimum_net_edge=Decimal("0.01"),
    )
    total_gbp, min_pnl, states = exact_frozen_settlement(
        matched, costs=costs, gbp_per_unit=fx
    )
    assert total_gbp > 0
    assert min_pnl > 0
    assert set(states) == {"home", "away"}
    frozen = freeze_native_orders(
        type("Decision", (), {"fill_legs": [mb, pm]})(),
        polymarket_books=_pm_books(),
    )
    by_venue = {item.venue: item for item in frozen}
    assert by_venue["matchbook"].ladder_odds == "2.00"
    assert by_venue["matchbook"].approved_depth_worst_limit == "2.00"
    assert by_venue["matchbook"].expected_average == "2.02"
    assert by_venue["matchbook"].admitted_levels == (("2.20", "0.50"), ("2.00", "4.50"))
    assert by_venue["polymarket"].limit_price == "0.4"
    assert by_venue["polymarket"].order_type == PM_FAK
    assert Decimal(by_venue["polymarket"].native_amount) == Decimal("6.25")
    assert Decimal(by_venue["matchbook"].native_stake) == Decimal("5.00")
    simulated = PaperFillSimulator().simulate_leg(
        mb,
        PaperFillConfig(mode=FillMode.REALISTIC, max_quote_age_ms=None),
        now=NOW,
    )
    assert simulated.fully_filled is True
    assert simulated.worst_odds == Decimal("2.00")
    assert simulated.weighted_odds == Decimal("2.02")


def test_unprofitable_second_level_downsizes_instead_of_submitting_five() -> None:
    sources = [
        DepthQuoteSource(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-mkt",
            source_runner_id="101",
            levels=[_level("2.20", "0.50"), _level("2.00", "100")],
            cost=_cost(VenueName.MATCHBOOK, action=MarketAction.BACK, rate="0.05"),
        ),
        DepthQuoteSource(
            outcome="away",
            venue=VenueName.POLYMARKET,
            source_market_id="pm-mkt",
            source_runner_id=TOKEN,
            levels=[_level("2.04", "1000")],
            cost=_cost(VenueName.POLYMARKET, action=MarketAction.BUY),
        ),
    ]
    result = DepthAwareCompleteSetScanner().scan(
        sources,
        expected_outcomes=["home", "away"],
    )
    assert result.solution.is_arbitrage is True
    mb = next(quote for quote in result.selected_quotes if quote.venue is VenueName.MATCHBOOK)
    assert mb.cumulative_depth == Decimal("0.50")
    assert mb.worst_limit_odds == Decimal("2.20")
    assert mb.levels_consumed == 1
    five = next(quote for quote in result.selected_quotes if quote.venue is VenueName.POLYMARKET)
    del five
    mb_leg = _mb_leg(stake="5.00")
    pm_leg = _pm_leg(stake="6.25", levels=[_level("2.04", "1000")])
    costs = {
        VenueName.MATCHBOOK: _cost(VenueName.MATCHBOOK, action=MarketAction.BACK, rate="0.05"),
        VenueName.POLYMARKET: _cost(VenueName.POLYMARKET, action=MarketAction.BUY),
    }
    assert (
        qualifies_at_conservative_limits(
            [mb_leg, pm_leg],
            costs=costs,
            gbp_per_unit={"GBP": Decimal(1), "USD": GBP_PER_USD},
            minimum_net_edge=Decimal("0.01"),
        )
        is False
    )


def test_sub_minimum_top_level_still_contributes_to_valid_sweep() -> None:
    thin = _pm_leg(stake="0.50", levels=[_level("2.222222222222222222", "0.50")])
    frozen_thin = freeze_native_orders(
        type("Decision", (), {"fill_legs": [thin]})(),
        polymarket_books=_pm_books(minimum="5"),
    )
    assert frozen_thin == ()
    combined = _pm_leg(
        stake="5.00",
        levels=[_level("2.222222222222222222", "0.50"), _level("2.00", "100")],
    )
    frozen = freeze_native_orders(
        type("Decision", (), {"fill_legs": [combined]})(),
        polymarket_books=_pm_books(minimum="5"),
    )
    assert len(frozen) == 1
    assert frozen[0].limit_price == "0.5"
    assert frozen[0].approved_depth_worst_limit == "2.00"
    assert Decimal(frozen[0].native_shares) >= Decimal(5)


def test_tick_mismatch_on_multi_level_plan_fails_closed() -> None:
    off_ladder = stamp_fill_leg_depth_plan(
        PaperOpportunityLeg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-mkt",
            source_runner_id="101",
            source_event_id="evt-mb",
            currency="GBP",
            requested_stake=Decimal("5.00"),
            displayed_odds=Decimal("2.20"),
            levels=[_level("2.20", "0.50"), _level("2.03", "100")],
        )
    )
    frozen = freeze_native_orders(type("Decision", (), {"fill_legs": [off_ladder]})())
    assert frozen == ()


def test_single_level_matchbook_freeze_still_snaps_the_ladder() -> None:
    leg = PaperOpportunityLeg(
        outcome="home",
        venue=VenueName.MATCHBOOK,
        source_market_id="mb-mkt",
        source_runner_id="101",
        source_event_id="evt-mb",
        currency="GBP",
        requested_stake=Decimal(4),
        displayed_odds=Decimal("2.09"),
    )
    frozen = freeze_native_orders(type("Decision", (), {"fill_legs": [leg]})())
    assert frozen[0].ladder_odds == "2.10"
    assert frozen[0].approved_depth_worst_limit == "2.09"


def test_allocation_resize_recomputes_worst_limit() -> None:
    resized = stamp_fill_leg_depth_plan(
        _mb_leg(stake="5.00").model_copy(update={"requested_stake": Decimal("0.50")})
    )
    assert resized.requested_stake == Decimal("0.50")
    assert resized.approved_depth_worst_limit == Decimal("2.20")


def test_prefix_candidates_qualify_on_worst_limit_not_blend() -> None:
    source = DepthQuoteSource(
        outcome="home",
        venue=VenueName.MATCHBOOK,
        source_market_id="mb-mkt",
        source_runner_id="101",
        levels=[_level("2.20", "0.50"), _level("2.00", "100")],
        cost=_cost(VenueName.MATCHBOOK, action=MarketAction.BACK),
    )
    candidates, _ = prefix_depth_candidates(source, configured_slippage_bps=None)
    deep = next(item for item in candidates if item.levels_consumed == 2)
    assert deep.gross_weighted_odds != deep.net_decimal_odds
    assert deep.worst_limit_odds == Decimal("2.00")
    assert deep.net_decimal_odds == Decimal("2.00")
    assert deep.expected_average_net_odds == deep.gross_weighted_odds
    assert deep.gross_weighted_odds > deep.worst_limit_odds


@pytest.mark.asyncio
async def test_partial_matchbook_remainder_is_not_a_successful_arb() -> None:
    class RestingTransport(DeterministicExecutionTransport):
        async def dispatch(self, request):
            result = await super().dispatch(request)
            return result.model_copy(
                update={
                    "status": VenueOrderStatus.PARTIAL,
                    "filled_size": Decimal("0.50"),
                    "remainder_quantity": Decimal("4.50"),
                    "venue_order_id": "offer-1",
                }
            )

        async def cancel(self, request):
            now = datetime.now(UTC)
            return VenueOrderResult(
                venue=request.venue,
                client_order_id=request.client_order_id,
                venue_order_id="offer-1",
                status=VenueOrderStatus.CANCELLED,
                requested_size=request.requested_size,
                filled_size=Decimal("0.50"),
                requested_price=request.requested_price,
                average_fill_price=request.requested_price,
                submitted_at=now,
                updated_at=now,
            )

    from sports_hedge.execution.models import VenueOrderRequest

    transport = RestingTransport(filled_fraction=Decimal("0.1"))
    client = MatchbookExecutionClient(transport)
    request = VenueOrderRequest(
        venue=VenueName.MATCHBOOK,
        trade_id="t1",
        tranche_id="opening",
        native_event_id="evt-mb",
        native_market_id="mb-mkt",
        native_runner_id="101",
        side=MarketSide.BACK,
        currency="GBP",
        requested_price=Decimal("2.00"),
        requested_size=Decimal("5.00"),
        client_order_id="sh-partial",
    )
    order = await client.dispatch(request)
    updated, remainder = await release_matchbook_remainders([order], [request], matchbook=client)
    assert remainder == "cancelled"
    assert updated[0].status is VenueOrderStatus.CANCELLED
    assert updated[0].filled_size == Decimal("0.50")
    assert updated[0].status is not VenueOrderStatus.FILLED


def test_consumed_best_level_is_not_spent_twice_on_the_next_tranche() -> None:
    opening = [_level("2.20", "0.50"), _level("2.00", "100")]
    leftover = remaining_book_levels(opening, Decimal("0.50"))
    assert leftover[0].decimal_odds == Decimal("2.00")
    assert leftover[0].available_stake == Decimal(100)
    plan = sweep_back_levels(leftover, Decimal("5.00"))
    assert plan.worst_limit_odds == Decimal("2.00")
    assert all(item.decimal_odds != Decimal("2.20") for item in plan.levels)


def test_execution_disabled_refuses_live_dispatch() -> None:
    settings = Settings()
    reason = refusal_reason(
        type(
            "Plan",
            (),
            {
                "legs": [_mb_leg(stake="5.00")],
                "execution_snapshot_json": None,
                "eligible_for_paper_simulation": True,
            },
        )(),
        settings=settings,
        matchbook=MatchbookExecutionClient(DeterministicExecutionTransport()),
        kalshi=None,
        polymarket=PolymarketExecutionClient(DeterministicExecutionTransport()),
    )
    assert reason == "execution_disabled"


def test_fill_legs_stamp_worst_limit_from_observations() -> None:
    from sports_hedge.arbitrage.depth import DepthQuoteCandidate, DepthScanResult
    from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake
    from sports_hedge.domain.football import CanonicalOutcome

    class _Book:
        def __init__(self, outcome, runner, levels):
            self.outcome = outcome
            self.source_runner_id = runner
            self.back_levels = levels

    class _Market:
        source_market_id = "mb-mkt"

        class event:
            source_event_id = "evt-mb"

    class _Obs:
        venue = VenueName.MATCHBOOK
        native_currency = "GBP"
        quote_age_ms = 10
        observed_at = NOW
        market = _Market()
        outcome_books = [
            _Book(
                CanonicalOutcome.HOME,
                "101",
                [_level("2.20", "0.50"), _level("2.00", "100")],
            )
        ]

    scan = DepthScanResult(
        solution=ArbitrageSolution(
            is_arbitrage=True,
            implied_probability_sum=Decimal("0.95"),
            stakes=[
                ArbitrageStake(
                    outcome="home",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb-mkt",
                    stake=Decimal("5.00"),
                    net_decimal_odds=Decimal("2.00"),
                    state_return=Decimal(10),
                )
            ],
        ),
        selected_quotes=[
            DepthQuoteCandidate(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb-mkt",
                source_runner_id="101",
                gross_weighted_odds=Decimal("2.02"),
                net_decimal_odds=Decimal("2.00"),
                cumulative_depth=Decimal("5.00"),
                levels_consumed=2,
                worst_limit_odds=Decimal("2.00"),
            )
        ],
    )
    # Pair with an empty right observation that has no matching selected quote.
    class _Right:
        venue = VenueName.POLYMARKET
        native_currency = "USD"
        quote_age_ms = 10
        observed_at = NOW
        market = _Market()
        outcome_books = []

    legs = _fill_legs_from_observations(
        _Obs(),
        _Right(),
        depth_scan=scan,
        effective_fx={"GBP": Decimal(1), "USD": GBP_PER_USD},
    )
    assert len(legs) == 1
    assert legs[0].requested_stake == Decimal("5.00")
    assert legs[0].displayed_odds == Decimal("2.20")
    assert legs[0].approved_depth_worst_limit == Decimal("2.00")


@pytest.mark.asyncio
async def test_run_live_opening_sends_nothing_when_disarmed() -> None:
    from sports_hedge.matching.markets import MarketMatchResult
    from sports_hedge.paper.chain import PaperFillPlan
    from sports_hedge.paper.models import PaperScanDecision

    legs = [_mb_leg(stake="5.00")]
    plan = PaperFillPlan(
        opportunity_id="watch:mkt",
        canonical_event_id="evt",
        canonical_market_id="mkt",
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        legs=legs,
        decision=PaperScanDecision(
            canonical_market_id="mkt",
            canonical_event_id="evt",
            eligible_for_paper_simulation=True,
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
            solver_model="simple_complete_set",
            fill_legs=legs,
            scanned_at=NOW,
        ),
        execution_snapshot_json='{"accepted":true,"snapshot_id":"exec:39"}',
    )
    result = await run_live_opening(
        plan,
        trade_id="trade-39",
        tranche_id="opening",
        settings=Settings(),
        matchbook=MatchbookExecutionClient(DeterministicExecutionTransport()),
        kalshi=None,
        polymarket=None,
    )
    assert isinstance(result, LiveDispatchResult)
    assert result.sent is False
    assert result.refusal == "execution_disabled"


def test_theoretical_solver_ratio_is_not_proof_for_the_frozen_pair() -> None:
    mb = _mb_leg(stake="10.00")
    pm = _pm_leg(stake="1.00", levels=[_level("2.10", "100")])
    costs = {
        VenueName.MATCHBOOK: _cost(VenueName.MATCHBOOK, action=MarketAction.BACK),
        VenueName.POLYMARKET: _cost(VenueName.POLYMARKET, action=MarketAction.BUY),
    }
    fx = {"GBP": Decimal(1), "USD": GBP_PER_USD}
    theoretical = conservative_complete_set_solution( [mb, pm], costs=costs, gbp_per_unit=fx)
    assert theoretical.is_arbitrage is True
    assert theoretical.roi >= Decimal("0.01")
    assert (
        qualifies_at_conservative_limits(
            [mb, pm],
            costs=costs,
            gbp_per_unit=fx,
            minimum_net_edge=Decimal("0.01"),
        )
        is False
    )
    _total, minimum, state_pnl = exact_frozen_settlement([mb, pm], costs=costs, gbp_per_unit=fx)
    assert minimum <= 0 or min(state_pnl.values()) < 0


def test_generalized_dnb_refund_state_is_included_in_exact_payoff() -> None:
    from sports_hedge.application.complete_set import GeneralizedStateModel

    mb = stamp_fill_leg_depth_plan(
        PaperOpportunityLeg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-dnb",
            source_runner_id="101",
            source_event_id="evt-mb",
            currency="GBP",
            requested_stake=Decimal("5.00"),
            displayed_odds=Decimal("2.20"),
            levels=[_level("2.20", "50")],
        )
    )
    pm = stamp_fill_leg_depth_plan(
        PaperOpportunityLeg(
            outcome="away",
            venue=VenueName.POLYMARKET,
            source_market_id="pm-dnb",
            source_runner_id=TOKEN,
            source_event_id="evt-pm",
            currency="USD",
            requested_stake=Decimal("6.25"),
            displayed_odds=Decimal("2.20"),
            levels=[_level("2.20", "50")],
        )
    )
    costs = {
        VenueName.MATCHBOOK: _cost(VenueName.MATCHBOOK, action=MarketAction.BACK),
        VenueName.POLYMARKET: _cost(VenueName.POLYMARKET, action=MarketAction.BUY),
    }
    fx = {"GBP": Decimal(1), "USD": GBP_PER_USD}
    total, minimum, states = exact_frozen_settlement(
        [mb, pm],
        costs=costs,
        gbp_per_unit=fx,
        state_model=GeneralizedStateModel.DRAW_NO_BET,
    )
    assert set(states) == {"home", "draw", "away"}
    assert states["draw"] == Decimal(0)
    assert total == Decimal("10.00")
    assert minimum > 0
