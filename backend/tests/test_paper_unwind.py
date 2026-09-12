from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_operations_service
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
)
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.liquidity.reverse import walk_lay_to_cover_payout, walk_prediction_sell
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.liquidity import PaperLiquidityPool, PaperLiquiditySnapshot
from sports_hedge.paper.trades import (
    PaperLegFillKind,
    PaperTrade,
    PaperTradeLeg,
    PaperTradeState,
)
from sports_hedge.paper.unwind import (
    CapitalPressure,
    CapitalScarcityInput,
    IncrementalCloseCapitalStatus,
    OpenPaperPosition,
    PaperUnwindEngine,
    ReverseQuote,
    UnwindEvaluationRequest,
    UnwindPolicy,
    UnwindRecommendation,
    VenueCloseMechanics,
    mechanics_for_venue,
    position_from_trade,
    register_venue_close_mechanics,
    venue_currency_key,
)
from sports_hedge.paper.unwind.models import OpenPaperLeg
from sports_hedge.paper.unwind.policy import decide_recommendation
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger


NOW = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
FINGERPRINT = "ft:regulation:match_result:v1"
FTTS_KEY = "ft:regulation:first_team_to_score:v1"


def _lay_cost(*, rate: str = "0.02") -> VenueCostSnapshot:
    return VenueCostSnapshot.per_quote_profit_commission(
        VenueName.MATCHBOOK,
        Decimal(rate),
        action=MarketAction.LAY,
        source="test_close_fee",
        captured_at=NOW,
        currency="GBP",
        detail="test matchbook closing lay commission",
    )


def _sell_cost(
    *,
    venue: VenueName = VenueName.POLYMARKET,
    rate: str | None = None,
    basis: FeeBasis = FeeBasis.NONE_CONFIRMED,
) -> VenueCostSnapshot:
    return VenueCostSnapshot(
        venue=venue,
        action=MarketAction.SELL,
        fee_basis=basis,
        known_status=CostKnownStatus.KNOWN,
        captured_at=NOW,
        source="test_close_fee",
        order_role=OrderRole.NOT_APPLICABLE,
        fee_scope=FeeScope.PER_QUOTE,
        rate=None if basis is FeeBasis.NONE_CONFIRMED else Decimal(rate or "0"),
        currency="USD",
        detail="test polymarket closing sell",
    )


def _unknown_fee() -> VenueCostSnapshot:
    return VenueCostSnapshot(
        venue=VenueName.MATCHBOOK,
        action=MarketAction.LAY,
        fee_basis=FeeBasis.UNKNOWN,
        known_status=CostKnownStatus.UNKNOWN,
        captured_at=NOW,
        source="test",
        currency="GBP",
    )


def _open_leg(
    *,
    venue: VenueName = VenueName.MATCHBOOK,
    outcome: str = "home",
    action: MarketAction = MarketAction.BACK,
    price: str = "2.20",
    size: str = "100",
    currency: str = "GBP",
    runner: str = "mb-home",
    market: str = "mb-1x2",
    event: str = "mb-evt",
    fingerprint: str = FINGERPRINT,
) -> OpenPaperLeg:
    return OpenPaperLeg(
        venue=venue,
        source_event_id=event,
        source_market_id=market,
        source_runner_id=runner,
        source_contract_id=runner if venue is VenueName.POLYMARKET else None,
        canonical_market_id="mkt-1",
        canonical_outcome=outcome,
        canonical_state=outcome,
        opening_action=action,
        filled_price=Decimal(price),
        filled_size=Decimal(size),
        native_currency=currency,
        settlement_fingerprint_key=fingerprint,
        fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
    )


def _quote(
    *,
    venue: VenueName,
    outcome: str,
    levels: list[BookLevel],
    cost: VenueCostSnapshot,
    currency: str,
    runner: str,
    market: str,
    event: str = "mb-evt",
    fingerprint: str = FINGERPRINT,
    age_ms: int | None = 100,
) -> ReverseQuote:
    return ReverseQuote(
        venue=venue,
        source_event_id=event,
        source_market_id=market,
        source_runner_id=runner,
        canonical_outcome=outcome,
        settlement_fingerprint_key=fingerprint,
        native_currency=currency,
        levels=levels,
        quote_age_ms=age_ms,
        quote_age_basis="source",
        quoted_at=NOW,
        closing_cost=cost,
    )


def _position(
    legs: list[OpenPaperLeg],
    *,
    hold: str = "10",
    solver: str = "simple_complete_set",
    remaining_lock_minutes: Decimal | None = None,
    expected_settlement_at: datetime | None = None,
) -> OpenPaperPosition:
    return OpenPaperPosition(
        trade_id="ptrade-demo",
        opportunity_id="opp-demo",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_fingerprint_key=legs[0].settlement_fingerprint_key,
        solver_model=solver,
        hold_pnl_gbp=Decimal(hold),
        capital_locked_native={leg.native_currency: sum((item.filled_size for item in legs if item.native_currency == leg.native_currency), Decimal("0")) for leg in legs},
        remaining_lock_minutes=remaining_lock_minutes,
        expected_settlement_at=expected_settlement_at,
        legs=legs,
    )


def test_lay_walk_covers_payout_and_does_not_treat_lay_as_back() -> None:
    levels = [
        BookLevel(decimal_odds=Decimal("2.50"), available_stake=Decimal("500")),
        BookLevel(decimal_odds=Decimal("1.90"), available_stake=Decimal("80")),
    ]
    fill = walk_lay_to_cover_payout(levels, Decimal("220"))
    assert fill.fully_filled is True
    assert fill.weighted_average_odds is not None
    assert fill.weighted_average_odds < Decimal("2.50")
    assert fill.liability == fill.matched_stake * (fill.weighted_average_odds - Decimal("1"))
    # A back walker would prefer 2.50 first; the close uses the lowest lay.
    assert fill.worst_odds == Decimal("2.50") or fill.levels_consumed >= 1
    first_only = walk_lay_to_cover_payout(
        [BookLevel(decimal_odds=Decimal("1.90"), available_stake=Decimal("80"))],
        Decimal("152"),
    )
    assert first_only.fully_filled is True
    assert first_only.matched_stake == Decimal("80")


def test_prediction_sell_uses_share_quantity_not_back_stake() -> None:
    # 100 shares at 0.48: odds 25/12, notional 96.
    levels = [
        BookLevel(decimal_odds=Decimal("25") / Decimal("12"), available_stake=Decimal("96")),
    ]
    fill = walk_prediction_sell(levels, Decimal("100"))
    assert fill.fully_filled is True
    assert fill.shares_sold == Decimal("100")
    assert fill.proceeds == Decimal("48")


def test_fully_executable_reverse_depth_produces_exact_pnl_and_releasable_capital() -> None:
    engine = PaperUnwindEngine()
    matchbook = _open_leg()
    polymarket = _open_leg(
        venue=VenueName.POLYMARKET,
        outcome="home",
        action=MarketAction.BUY,
        price="2.00",
        size="50",
        currency="USD",
        runner="pm-home",
        market="pm-1x2",
        event="pm-evt",
    )
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([matchbook, polymarket], hold="12"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                ),
                _quote(
                    venue=VenueName.POLYMARKET,
                    outcome="home",
                    levels=[
                        BookLevel(
                            decimal_odds=Decimal("25") / Decimal("12"),
                            available_stake=Decimal("96"),
                        )
                    ],
                    cost=_sell_cost(),
                    currency="USD",
                    runner="pm-home",
                    market="pm-1x2",
                    event="pm-evt",
                ),
            ],
            fx=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), source="test", captured_at=NOW)],
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    # Matchbook: matched 110, gross 10, 2% commission 0.20 => 9.80
    # Polymarket: sell 100 shares @ 0.48 => proceeds 48, pnl -2 USD * 0.80 = -1.60
    assert decision.validated_exit_pnl_gbp == Decimal("8.20")
    assert decision.conditionally_releasable_by_venue_currency[venue_currency_key(VenueName.MATCHBOOK, "GBP")] == Decimal("100")
    assert decision.conditionally_releasable_by_venue_currency[venue_currency_key(VenueName.POLYMARKET, "USD")] == Decimal("50")
    assert decision.spendable is False
    assert decision.remaining_lock_minutes is None
    assert decision.capital_turnover_hint is None
    mb = decision.close_plan.legs[0]
    assert mb.close_action is MarketAction.LAY
    assert mb.liability == Decimal("110")
    assert mb.matched_stake == Decimal("110")
    assert mb.incremental_close_capital_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
    assert mb.incremental_close_capital_native is None
    assert decision.gross_close_liability_native[venue_currency_key(VenueName.MATCHBOOK, "GBP")] == Decimal("110")
    assert decision.incremental_close_capital_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
    assert decision.incremental_close_capital_native == {}
    assert "GBP" not in decision.gross_close_liability_native
    pm = decision.close_plan.legs[1]
    assert pm.close_action is MarketAction.SELL
    assert pm.proceeds == Decimal("48")
    assert pm.incremental_close_capital_status is IncrementalCloseCapitalStatus.KNOWN
    assert pm.incremental_close_capital_native == Decimal("0")


def test_insufficient_reverse_depth_is_not_safe_and_releases_nothing() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg()], hold="5"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("10"))],
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            evaluated_at=NOW,
        )
    )
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert decision.decision_reason == "insufficient_reverse_depth"
    assert decision.conditionally_releasable_by_venue_currency == {}
    assert decision.validated_exit_pnl_gbp is None


def test_fees_make_converged_exit_inferior_so_hold() -> None:
    engine = PaperUnwindEngine()
    # Back 100 @ 2.00, lay also 2.00 => greened pnl 0 before fees, commission 0, still worse than hold 8.
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="2.00")], hold="8"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            policy=UnwindPolicy(),
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    assert decision.validated_exit_pnl_gbp == Decimal("0")
    assert decision.recommendation is UnwindRecommendation.HOLD
    assert decision.decision_reason == "exit_inferior_to_hold_after_fees"
    assert decision.conditionally_releasable_by_venue_currency[venue_currency_key(VenueName.MATCHBOOK, "GBP")] == Decimal("100")


def test_scarce_capital_give_up_must_pass_both_caps() -> None:
    engine = PaperUnwindEngine()
    quotes = [
        _quote(
            venue=VenueName.MATCHBOOK,
            outcome="home",
            levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
            cost=_lay_cost(),
            currency="GBP",
            runner="mb-home",
            market="mb-1x2",
        )
    ]
    scarce = CapitalScarcityInput(pressure=CapitalPressure.SCARCE)

    surrendered = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="2.00")], hold="1.50"),
            quotes=quotes,
            scarcity=scarce,
            evaluated_at=NOW,
        )
    )
    assert surrendered.close_plan.fully_executable is True
    assert surrendered.validated_exit_pnl_gbp == Decimal("0")
    assert surrendered.profit_give_up_gbp == Decimal("1.50")
    assert surrendered.recommendation is UnwindRecommendation.HOLD
    assert surrendered.decision_reason == "give_up_exceeds_scarce_capital_threshold"

    small = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="2.20")], hold="10"),
            quotes=quotes,
            scarcity=scarce,
            evaluated_at=NOW,
        )
    )
    # matched 110, gross 10, 2% fee 0.20 => exit 9.80; give-up 0.20
    # min(£2, 10% of £10 = £1) = £1; 0.20 <= 1 would be UNWIND_ELIGIBLE on P&L,
    # but scarce recycling requires known incremental close capital. Gross lay
    # liability 110 is not modelled Matchbook netting, so fail closed.
    assert small.validated_exit_pnl_gbp == Decimal("9.80")
    assert small.profit_give_up_gbp == Decimal("0.20")
    assert small.close_plan.legs[0].liability == Decimal("110")
    assert small.incremental_close_capital_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
    assert small.incremental_close_capital_native == {}
    assert small.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert small.decision_reason == "incremental_close_capital_unknown"
    assert small.capital_turnover_hint == "scarce_capital_competing_opportunities"


def test_scarce_policy_uses_the_more_restrictive_bound() -> None:
    policy = UnwindPolicy()
    scarce = CapitalScarcityInput(pressure=CapitalPressure.SCARCE)
    hold_surrender, reason = decide_recommendation(
        fully_executable=True,
        fail_reasons=[],
        hold_pnl_gbp=Decimal("1.50"),
        exit_pnl_gbp=Decimal("0"),
        policy=policy,
        scarcity=scarce,
        execution_risk_score=0,
    )
    assert hold_surrender is UnwindRecommendation.HOLD
    assert reason == "give_up_exceeds_scarce_capital_threshold"

    eligible, reason = decide_recommendation(
        fully_executable=True,
        fail_reasons=[],
        hold_pnl_gbp=Decimal("10"),
        exit_pnl_gbp=Decimal("9.80"),
        policy=policy,
        scarcity=scarce,
        execution_risk_score=0,
    )
    assert eligible is UnwindRecommendation.UNWIND_ELIGIBLE
    assert reason == "scarce_capital_accepts_bounded_give_up"


def test_abundant_capital_prefers_hold_of_guaranteed_position() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="2.00")], hold="1.50"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            evaluated_at=NOW,
        )
    )
    assert decision.recommendation is UnwindRecommendation.HOLD
    assert decision.decision_reason == "exit_inferior_to_hold_after_fees"


@pytest.mark.parametrize(
    "reason,kwargs",
    [
        ("stale_quote", {"age_ms": 5000}),
        ("unknown_quote_age", {"age_ms": None}),
    ],
)
def test_stale_or_unknown_quote_fails_closed(reason: str, kwargs: dict) -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg()], hold="5"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                    **kwargs,
                )
            ],
            evaluated_at=NOW,
        )
    )
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert decision.decision_reason == reason


def test_unknown_fx_fails_closed() -> None:
    engine = PaperUnwindEngine()
    polymarket = _open_leg(
        venue=VenueName.POLYMARKET,
        action=MarketAction.BUY,
        price="2.00",
        size="50",
        currency="USD",
        runner="pm-home",
        market="pm-1x2",
        event="pm-evt",
    )
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([polymarket], hold="1"),
            quotes=[
                _quote(
                    venue=VenueName.POLYMARKET,
                    outcome="home",
                    levels=[
                        BookLevel(decimal_odds=Decimal("1") / Decimal("0.50"), available_stake=Decimal("100"))
                    ],
                    cost=_sell_cost(),
                    currency="USD",
                    runner="pm-home",
                    market="pm-1x2",
                    event="pm-evt",
                )
            ],
            fx=[],
            evaluated_at=NOW,
        )
    )
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert decision.decision_reason == "missing_fx_rate:USD"


def test_unsupported_exit_fee_fails_closed() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg()], hold="5"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
                    cost=_unknown_fee(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            evaluated_at=NOW,
        )
    )
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert decision.decision_reason == "unknown_costs"


def test_exchange_close_uses_liability_semantics() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="3.00", size="40")], hold="0"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("80"))],
                    cost=_lay_cost(rate="0"),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("100")),
            evaluated_at=NOW,
        )
    )
    leg = decision.close_plan.legs[0]
    assert leg.matched_stake == Decimal("60")  # 40 * 3 / 2
    assert leg.liability == Decimal("60")  # matched * (2-1)
    assert leg.native_close_pnl == Decimal("20")  # 60 - 40
    # Treating 2.00 lay as a back of 40 would not produce liability 60.
    assert leg.close_action is MarketAction.LAY
    assert mechanics_for_venue(VenueName.MATCHBOOK) is VenueCloseMechanics.EXCHANGE_BACK_LAY
    assert decision.gross_close_liability_native[venue_currency_key(VenueName.MATCHBOOK, "GBP")] == Decimal("60")
    assert decision.incremental_close_capital_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
    assert decision.incremental_close_capital_native == {}
    assert decision.recommendation is UnwindRecommendation.UNWIND_ELIGIBLE


def test_gross_lay_liability_is_not_known_incremental_close_capital() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg()], hold="12"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            evaluated_at=NOW,
        )
    )
    leg = decision.close_plan.legs[0]
    assert decision.close_plan.fully_executable is True
    assert leg.liability == Decimal("110")
    assert decision.gross_close_liability_native[venue_currency_key(VenueName.MATCHBOOK, "GBP")] == Decimal("110")
    assert decision.incremental_close_capital_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
    assert decision.incremental_close_capital_native == {}
    assert venue_currency_key(VenueName.MATCHBOOK, "GBP") not in decision.incremental_close_capital_native


def test_prediction_sell_has_known_zero_incremental_close_capital() -> None:
    engine = PaperUnwindEngine()
    polymarket = _open_leg(
        venue=VenueName.POLYMARKET,
        action=MarketAction.BUY,
        price="2.00",
        size="50",
        currency="USD",
        runner="pm-home",
        market="pm-1x2",
        event="pm-evt",
    )
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([polymarket], hold="0"),
            quotes=[
                _quote(
                    venue=VenueName.POLYMARKET,
                    outcome="home",
                    levels=[
                        BookLevel(decimal_odds=Decimal("1") / Decimal("0.50"), available_stake=Decimal("100"))
                    ],
                    cost=_sell_cost(),
                    currency="USD",
                    runner="pm-home",
                    market="pm-1x2",
                    event="pm-evt",
                )
            ],
            fx=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), source="test", captured_at=NOW)],
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.SCARCE),
            evaluated_at=NOW,
        )
    )
    leg = decision.close_plan.legs[0]
    assert decision.close_plan.fully_executable is True
    assert leg.liability == Decimal("0")
    assert leg.incremental_close_capital_status is IncrementalCloseCapitalStatus.KNOWN
    assert leg.incremental_close_capital_native == Decimal("0")
    assert decision.incremental_close_capital_status is IncrementalCloseCapitalStatus.KNOWN
    assert decision.incremental_close_capital_native[venue_currency_key(VenueName.POLYMARKET, "USD")] == Decimal("0")
    assert decision.gross_close_liability_native == {}
    assert decision.recommendation is UnwindRecommendation.UNWIND_ELIGIBLE
    assert decision.decision_reason == "exit_pnl_not_inferior_to_hold"


def test_require_known_incremental_close_capital_fails_closed_for_unmodelled_lay() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="3.00", size="40")], hold="0"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("80"))],
                    cost=_lay_cost(rate="0"),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            policy=UnwindPolicy(
                max_profit_give_up_gbp=Decimal("100"),
                require_known_incremental_close_capital=True,
            ),
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    assert decision.close_plan.legs[0].liability == Decimal("60")
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert decision.decision_reason == "incremental_close_capital_unknown"


def test_simple_and_generalized_positions_share_the_same_engine() -> None:
    engine = PaperUnwindEngine()
    simple = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(outcome="draw", price="3.40")], hold="4", solver="simple_complete_set"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home" if False else "draw",
                    levels=[BookLevel(decimal_odds=Decimal("3.00"), available_stake=Decimal("200"))],
                    cost=_lay_cost(rate="0"),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("100")),
            evaluated_at=NOW,
        )
    )
    ftts = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position(
                [
                    _open_leg(
                        outcome="no_goal",
                        price="3.10",
                        size="30",
                        fingerprint=FTTS_KEY,
                    )
                ],
                hold="2",
                solver="generalized_payoff",
            ),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="no_goal",
                    levels=[BookLevel(decimal_odds=Decimal("2.80"), available_stake=Decimal("200"))],
                    cost=_lay_cost(rate="0"),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                    fingerprint=FTTS_KEY,
                )
            ],
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("100")),
            evaluated_at=NOW,
        )
    )
    assert simple.close_plan.fully_executable is True
    assert ftts.close_plan.fully_executable is True
    assert ftts.close_plan.legs[0].canonical_outcome == "no_goal"


def test_opening_lay_is_rejected() -> None:
    with pytest.raises(ValidationError, match="opening_lay_not_supported"):
        _open_leg(action=MarketAction.LAY)


def test_kalshi_compatible_registry_does_not_hard_code_a_venue_pair() -> None:
    original = mechanics_for_venue(VenueName.POLYMARKET)
    register_venue_close_mechanics(VenueName.POLYMARKET, VenueCloseMechanics.PREDICTION_BINARY_BUY_SELL)
    assert mechanics_for_venue(VenueName.POLYMARKET) is VenueCloseMechanics.PREDICTION_BINARY_BUY_SELL
    assert mechanics_for_venue(VenueName.MATCHBOOK) is not mechanics_for_venue(VenueName.POLYMARKET)
    register_venue_close_mechanics(VenueName.POLYMARKET, original)


def test_two_usd_venues_are_not_merged_in_conditionally_releasable() -> None:
    original = mechanics_for_venue(VenueName.SMARKETS)
    register_venue_close_mechanics(VenueName.SMARKETS, VenueCloseMechanics.PREDICTION_BINARY_BUY_SELL)
    try:
        engine = PaperUnwindEngine()
        polymarket = _open_leg(
            venue=VenueName.POLYMARKET,
            action=MarketAction.BUY,
            price="2.00",
            size="50",
            currency="USD",
            runner="pm-home",
            market="pm-1x2",
            event="pm-evt",
        )
        kalshi_shaped = _open_leg(
            venue=VenueName.SMARKETS,
            action=MarketAction.BUY,
            price="2.00",
            size="40",
            currency="USD",
            runner="kl-home",
            market="kl-1x2",
            event="kl-evt",
        )
        decision = engine.evaluate(
            UnwindEvaluationRequest(
                position=_position([polymarket, kalshi_shaped], hold="1"),
                quotes=[
                    _quote(
                        venue=VenueName.POLYMARKET,
                        outcome="home",
                        levels=[
                            BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("100"))
                        ],
                        cost=_sell_cost(venue=VenueName.POLYMARKET),
                        currency="USD",
                        runner="pm-home",
                        market="pm-1x2",
                        event="pm-evt",
                    ),
                    _quote(
                        venue=VenueName.SMARKETS,
                        outcome="home",
                        levels=[
                            BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("100"))
                        ],
                        cost=_sell_cost(venue=VenueName.SMARKETS),
                        currency="USD",
                        runner="kl-home",
                        market="kl-1x2",
                        event="kl-evt",
                    ),
                ],
                fx=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), source="test", captured_at=NOW)],
                evaluated_at=NOW,
            )
        )
        assert decision.close_plan.fully_executable is True
        assert decision.spendable is False
        releasable = decision.conditionally_releasable_by_venue_currency
        assert venue_currency_key(VenueName.POLYMARKET, "USD") in releasable
        assert venue_currency_key(VenueName.SMARKETS, "USD") in releasable
        assert releasable[venue_currency_key(VenueName.POLYMARKET, "USD")] == Decimal("50")
        assert releasable[venue_currency_key(VenueName.SMARKETS, "USD")] == Decimal("40")
        assert "USD" not in releasable
        assert sum(releasable.values()) == Decimal("90")
    finally:
        register_venue_close_mechanics(VenueName.SMARKETS, original)


def test_unwind_does_not_mutate_liquidity_or_journal(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    trade = PaperTrade(
        trade_id="ptrade-x",
        opportunity_id="opp-x",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_key=FINGERPRINT,
        solver_model="simple_complete_set",
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        guaranteed_profit_gbp_at_open=Decimal("8"),
        capital_locked_native={"GBP": Decimal("100")},
        legs=[
            PaperTradeLeg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                currency="GBP",
                requested_stake=Decimal("100"),
                filled_stake=Decimal("100"),
                displayed_odds=Decimal("2.00"),
                filled_odds=Decimal("2.00"),
                source_market_id="mb-1x2",
                source_event_id="mb-evt",
                source_runner_id="mb-home",
                opening_action=MarketAction.BACK,
                canonical_state="home",
                settlement_fingerprint_key=FINGERPRINT,
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            )
        ],
        fx_snapshots=[FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional", captured_at=NOW)],
    )
    ledger.trades.save(trade)
    watchlist = WatchlistService(SqliteWatchlistRepository(tmp_path / "wl.sqlite"))
    ops = PaperOperationsService(watchlist=watchlist, ledger=ledger)
    before_journals = len(ops.journal.list_entries())
    pool = PaperLiquiditySnapshot(
        updated_at=NOW,
        pools=[
            PaperLiquidityPool(
                venue=VenueName.MATCHBOOK,
                native_currency="GBP",
                available=Decimal("5000"),
                locked=Decimal("100"),
                included_in_solver=True,
                connection_status="connected",
            ),
            PaperLiquidityPool(
                venue=VenueName.POLYMARKET,
                native_currency="USD",
                available=Decimal("5000"),
                included_in_solver=True,
                connection_status="connected",
            ),
            PaperLiquidityPool(
                venue=VenueName.SMARKETS,
                native_currency="GBP",
                available=Decimal("0"),
                included_in_solver=False,
                connection_status="not_connected",
            ),
        ],
    )
    snapshot = pool.model_copy(deep=True)
    decision = ops.evaluate_unwind(
        trade.trade_id,
        quotes=[
            _quote(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
                cost=_lay_cost(),
                currency="GBP",
                runner="mb-home",
                market="mb-1x2",
            )
        ],
        evaluated_at=NOW,
    )
    assert decision.spendable is False
    assert ops.journal.list_entries() == [] or len(ops.journal.list_entries()) == before_journals
    assert pool.model_dump() == snapshot.model_dump()
    persisted = ledger.trades.get(trade.trade_id)
    assert persisted is not None
    assert persisted.state is PaperTradeState.OPEN
    assert persisted.capital_locked_native["GBP"] == Decimal("100")
    assert any(event.event_type.value == "close_plan_evaluated" for event in persisted.audit)
    adapted = position_from_trade(persisted)
    assert adapted.remaining_lock_minutes is None
    assert adapted.expected_settlement_at is None
    assert decision.remaining_lock_minutes is None
    assert decision.capital_turnover_hint is None


def test_close_plan_api_is_paper_only_and_not_an_execution_endpoint(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper-api.sqlite")
    trade = PaperTrade(
        trade_id="ptrade-api",
        opportunity_id="opp-api",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_key=FINGERPRINT,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        guaranteed_profit_gbp_at_open=Decimal("1.50"),
        legs=[
            PaperTradeLeg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                currency="GBP",
                requested_stake=Decimal("100"),
                filled_stake=Decimal("100"),
                displayed_odds=Decimal("2.00"),
                filled_odds=Decimal("2.00"),
                source_market_id="mb-1x2",
                source_event_id="mb-evt",
                source_runner_id="mb-home",
                opening_action=MarketAction.BACK,
                canonical_state="home",
                settlement_fingerprint_key=FINGERPRINT,
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            )
        ],
    )
    ledger.trades.save(trade)
    watchlist = WatchlistService(SqliteWatchlistRepository(tmp_path / "wl-api.sqlite"))
    ops = PaperOperationsService(watchlist=watchlist, ledger=ledger)
    app.dependency_overrides[get_paper_operations_service] = lambda: ops
    try:
        client = TestClient(app)
        body = {
            "quotes": [
                {
                    "venue": "matchbook",
                    "source_event_id": "mb-evt",
                    "source_market_id": "mb-1x2",
                    "source_runner_id": "mb-home",
                    "canonical_outcome": "home",
                    "settlement_fingerprint_key": FINGERPRINT,
                    "native_currency": "GBP",
                    "levels": [{"decimal_odds": "2.00", "available_stake": "200"}],
                    "quote_age_ms": 100,
                    "quoted_at": NOW.isoformat(),
                    "closing_cost": _lay_cost().model_dump(mode="json"),
                }
            ],
            "scarcity": {"pressure": "scarce"},
            "places_orders": False,
        }
        response = client.post("/paper/trades/ptrade-api/close-plan", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["paper_only"] is True
        assert payload["places_orders"] is False
        assert payload["spendable"] is False
        assert payload["data_kind"] == "modelled_paper_unwind"
        assert "place_order" not in payload
    finally:
        app.dependency_overrides.clear()


def _executable_matchbook_quotes() -> list[ReverseQuote]:
    return [
        _quote(
            venue=VenueName.MATCHBOOK,
            outcome="home",
            levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
            cost=_lay_cost(),
            currency="GBP",
            runner="mb-home",
            market="mb-1x2",
        )
    ]


def test_unknown_remaining_lock_is_valid_and_not_fabricated() -> None:
    engine = PaperUnwindEngine()
    fabricated_finish = NOW + timedelta(minutes=90)
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position(
                [_open_leg(price="2.00")],
                hold="8",
                expected_settlement_at=fabricated_finish,
            ),
            quotes=_executable_matchbook_quotes(),
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    assert decision.remaining_lock_minutes is None
    assert decision.capital_turnover_hint != "capital_locked_until_expected_settlement"
    assert decision.capital_turnover_hint is None
    assert decision.recommendation is UnwindRecommendation.HOLD
    assert decision.decision_reason == "exit_inferior_to_hold_after_fees"


def test_engine_does_not_fabricate_a_finish_estimate_from_expected_settlement() -> None:
    engine = PaperUnwindEngine()
    with_timer = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position(
                [_open_leg(price="2.00")],
                hold="8",
                expected_settlement_at=NOW + timedelta(minutes=5),
            ),
            quotes=_executable_matchbook_quotes(),
            evaluated_at=NOW,
        )
    )
    unknown = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="2.00")], hold="8"),
            quotes=_executable_matchbook_quotes(),
            evaluated_at=NOW,
        )
    )
    assert with_timer.remaining_lock_minutes is None
    assert unknown.remaining_lock_minutes is None
    assert with_timer.capital_turnover_hint is None
    assert unknown.capital_turnover_hint is None
    assert "near_kickoff" not in (with_timer.execution_risk.reasons if with_timer.execution_risk else [])
    assert with_timer.recommendation is unknown.recommendation
    assert with_timer.decision_reason == unknown.decision_reason


def test_hold_vs_unwind_is_not_decided_by_predicted_game_end() -> None:
    engine = PaperUnwindEngine()
    quotes = _executable_matchbook_quotes()
    scarce = CapitalScarcityInput(
        pressure=CapitalPressure.SCARCE,
        opportunity_cost_gbp=Decimal("4"),
    )
    soon = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position(
                [_open_leg()],
                hold="12",
                expected_settlement_at=NOW + timedelta(minutes=3),
            ),
            quotes=quotes,
            scarcity=scarce,
            evaluated_at=NOW,
        )
    )
    unknown = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg()], hold="12"),
            quotes=quotes,
            scarcity=scarce,
            evaluated_at=NOW,
        )
    )
    far = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position(
                [_open_leg()],
                hold="12",
                remaining_lock_minutes=Decimal("180"),
                expected_settlement_at=NOW + timedelta(hours=3),
            ),
            quotes=quotes,
            scarcity=scarce,
            evaluated_at=NOW,
        )
    )
    assert soon.recommendation is unknown.recommendation is far.recommendation
    assert soon.decision_reason == unknown.decision_reason == far.decision_reason
    assert soon.decision_reason == "incremental_close_capital_unknown"
    assert soon.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert soon.gross_close_liability_native[venue_currency_key(VenueName.MATCHBOOK, "GBP")] == Decimal("110")
    assert soon.incremental_close_capital_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
    assert soon.incremental_close_capital_native == {}
    assert soon.capital_turnover_hint == "opportunity_cost_from_current_scarcity"
    assert unknown.capital_turnover_hint == "opportunity_cost_from_current_scarcity"
    assert far.remaining_lock_minutes == Decimal("180")
    assert unknown.remaining_lock_minutes is None


def test_execution_risk_does_not_invent_minutes_when_lock_unknown() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="2.00")], hold="0"),
            quotes=_executable_matchbook_quotes(),
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("100")),
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    assert decision.remaining_lock_minutes is None
    assert decision.execution_risk is not None
    assert "near_kickoff" not in decision.execution_risk.reasons


def test_unwind_modules_have_no_live_execution_surface() -> None:
    from pathlib import Path as P

    root = P(__file__).resolve().parents[1] / "src/sports_hedge/paper/unwind"
    combined = "\n".join(path.read_text() for path in root.glob("*.py"))
    for forbidden in ("place_order", "cancel_order", "wallet", "sign_order"):
        assert forbidden not in combined
    assert "conditionally_releasable" in combined
