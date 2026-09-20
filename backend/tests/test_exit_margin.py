from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
from sports_hedge.paper.position_management.manager import snapshot_from_decision
from sports_hedge.paper.position_management.models import PositionManagementAutoAction
from sports_hedge.paper.trades import PaperLegFillKind
from sports_hedge.paper.unwind import (
    CapitalPressure,
    CapitalScarcityInput,
    ExitMarginBasis,
    OpenPaperPosition,
    PaperUnwindEngine,
    ReverseQuote,
    UnwindEvaluationRequest,
    UnwindPolicy,
    UnwindRecommendation,
)
from sports_hedge.paper.unwind.models import OpenPaperLeg
from sports_hedge.paper.unwind.policy import decide_recommendation, evaluate_exit_economics
from sports_hedge.risk.execution import ExecutionRiskInputs, ExecutionRiskResult


NOW = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
FINGERPRINT = "ft:regulation:match_result:v1"


def _policy(**kwargs) -> UnwindPolicy:
    return UnwindPolicy(**kwargs)


def _economics(**kwargs):
    defaults = dict(
        fully_executable=True,
        fail_reasons=[],
        hold_pnl_gbp=Decimal("1.25"),
        exit_pnl_gbp=Decimal("0.84"),
        policy=_policy(max_profit_give_up_gbp=Decimal("0.20")),
        scarcity=CapitalScarcityInput(),
        execution_risk_score=0,
    )
    defaults.update(kwargs)
    return evaluate_exit_economics(**defaults)


def test_hold_negative_exit_margin_uses_abundant_threshold() -> None:
    economics = _economics()
    recommendation, reason = decide_recommendation(
        fully_executable=True,
        fail_reasons=[],
        hold_pnl_gbp=Decimal("1.25"),
        exit_pnl_gbp=Decimal("0.84"),
        policy=_policy(max_profit_give_up_gbp=Decimal("0.20")),
        scarcity=CapitalScarcityInput(),
        execution_risk_score=0,
    )
    assert recommendation is UnwindRecommendation.HOLD
    assert reason == "exit_inferior_to_hold_after_fees"
    assert economics.recommendation is recommendation
    assert economics.reason == reason
    assert economics.exit_threshold_gbp == Decimal("0.20")
    assert economics.exit_margin_gbp == Decimal("-0.21")
    assert economics.exit_margin_basis is ExitMarginBasis.ABUNDANT_CAPITAL_GIVE_UP
    assert economics.exit_margin_actionable is True
    assert economics.close_blocker is None


def test_eligible_non_negative_exit_margin_uses_abundant_threshold() -> None:
    economics = _economics(exit_pnl_gbp=Decimal("1.12"))
    assert economics.recommendation is UnwindRecommendation.UNWIND_ELIGIBLE
    assert economics.reason == "give_up_within_abundant_threshold"
    assert economics.exit_threshold_gbp == Decimal("0.20")
    assert economics.exit_margin_gbp == Decimal("0.07")
    assert economics.exit_margin_basis is ExitMarginBasis.ABUNDANT_CAPITAL_GIVE_UP
    assert economics.exit_margin_actionable is True
    assert economics.close_blocker is None


def test_opportunity_cost_basis_is_the_supplied_allocator_figure() -> None:
    scarce = CapitalScarcityInput(
        pressure=CapitalPressure.SCARCE,
        opportunity_cost_gbp=Decimal("0.50"),
    )
    eligible = _economics(
        hold_pnl_gbp=Decimal("10"),
        exit_pnl_gbp=Decimal("9.90"),
        policy=_policy(),
        scarcity=scarce,
    )
    assert eligible.recommendation is UnwindRecommendation.UNWIND_ELIGIBLE
    assert eligible.reason == "unwind_cost_within_supplied_opportunity_cost"
    assert eligible.exit_threshold_gbp == Decimal("0.50")
    assert eligible.exit_margin_gbp == Decimal("0.40")
    assert eligible.exit_margin_basis is ExitMarginBasis.SUPPLIED_OPPORTUNITY_COST

    hold = _economics(
        hold_pnl_gbp=Decimal("10"),
        exit_pnl_gbp=Decimal("9.40"),
        policy=_policy(),
        scarcity=CapitalScarcityInput(
            pressure=CapitalPressure.SCARCE,
            opportunity_cost_gbp=Decimal("0.50"),
        ),
    )
    assert hold.recommendation is UnwindRecommendation.HOLD
    assert hold.reason == "unwind_cost_exceeds_supplied_opportunity_cost"
    assert hold.exit_margin_gbp == Decimal("-0.10")
    assert hold.exit_margin_basis is ExitMarginBasis.SUPPLIED_OPPORTUNITY_COST


def test_scarce_capital_threshold_is_the_more_restrictive_bound() -> None:
    scarce = CapitalScarcityInput(pressure=CapitalPressure.SCARCE)
    # hold 1.50, 10% = 0.15, absolute cap 2 → allowed 0.15; give-up 1.50
    hold = _economics(
        hold_pnl_gbp=Decimal("1.50"),
        exit_pnl_gbp=Decimal("0"),
        policy=_policy(),
        scarcity=scarce,
    )
    assert hold.recommendation is UnwindRecommendation.HOLD
    assert hold.reason == "give_up_exceeds_scarce_capital_threshold"
    assert hold.exit_threshold_gbp == Decimal("0.15")
    assert hold.exit_margin_gbp == Decimal("-1.35")
    assert hold.exit_margin_basis is ExitMarginBasis.SCARCE_CAPITAL_BOUNDED_GIVE_UP

    eligible = _economics(
        hold_pnl_gbp=Decimal("10"),
        exit_pnl_gbp=Decimal("9.80"),
        policy=_policy(),
        scarcity=scarce,
    )
    assert eligible.recommendation is UnwindRecommendation.UNWIND_ELIGIBLE
    assert eligible.reason == "scarce_capital_accepts_bounded_give_up"
    assert eligible.exit_threshold_gbp == Decimal("1.00")
    assert eligible.exit_margin_gbp == Decimal("0.80")
    assert eligible.exit_margin_basis is ExitMarginBasis.SCARCE_CAPITAL_BOUNDED_GIVE_UP


def test_min_retained_exit_pnl_gate_is_the_policy_branch() -> None:
    economics = _economics(
        hold_pnl_gbp=Decimal("10"),
        exit_pnl_gbp=Decimal("9.50"),
        policy=_policy(
            max_profit_give_up_gbp=Decimal("1.00"),
            min_retained_exit_pnl_gbp=Decimal("9.80"),
        ),
    )
    assert economics.recommendation is UnwindRecommendation.HOLD
    assert economics.reason == "exit_below_minimum_retained_profit"
    assert economics.exit_threshold_gbp == Decimal("0.20")
    assert economics.exit_margin_gbp == Decimal("-0.30")
    assert economics.exit_margin_basis is ExitMarginBasis.MIN_RETAINED_EXIT_PNL


def test_not_safe_unavailable_close_plan_has_na_margin_and_blocker() -> None:
    economics = evaluate_exit_economics(
        fully_executable=False,
        fail_reasons=["missing_reverse_quote"],
        hold_pnl_gbp=Decimal("1.25"),
        exit_pnl_gbp=Decimal("1.25"),
        policy=_policy(max_profit_give_up_gbp=Decimal("0.20")),
        scarcity=CapitalScarcityInput(),
        execution_risk_score=None,
    )
    assert economics.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert economics.reason == "missing_reverse_quote"
    assert economics.exit_margin_gbp is None
    assert economics.exit_threshold_gbp is None
    assert economics.exit_margin_basis is ExitMarginBasis.UNAVAILABLE
    assert economics.exit_margin_actionable is False
    assert economics.close_blocker == "missing_reverse_quote"


def test_execution_risk_blocker_cannot_be_overridden_by_positive_economic_margin() -> None:
    economics = _economics(
        hold_pnl_gbp=Decimal("1.25"),
        exit_pnl_gbp=Decimal("1.12"),
        policy=_policy(max_profit_give_up_gbp=Decimal("0.20"), max_execution_risk=60),
        execution_risk_score=90,
    )
    recommendation, reason = decide_recommendation(
        fully_executable=True,
        fail_reasons=[],
        hold_pnl_gbp=Decimal("1.25"),
        exit_pnl_gbp=Decimal("1.12"),
        policy=_policy(max_profit_give_up_gbp=Decimal("0.20"), max_execution_risk=60),
        scarcity=CapitalScarcityInput(),
        execution_risk_score=90,
    )
    assert recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert reason == "close_execution_risk_exceeded"
    assert economics.recommendation is recommendation
    assert economics.exit_margin_gbp == Decimal("0.07")
    assert economics.exit_threshold_gbp == Decimal("0.20")
    assert economics.exit_margin_actionable is False
    assert economics.close_blocker == "close_execution_risk_exceeded"
    assert economics.recommendation is not UnwindRecommendation.UNWIND_ELIGIBLE


def test_engine_missing_reverse_quote_preserves_consideration_without_actionable_margin() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg()], hold="1.25"),
            quotes=[
                _quote(
                    venue=VenueName.POLYMARKET,
                    outcome="away",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))],
                    cost=_sell_cost(),
                    currency="USD",
                    runner="pm-away",
                    market="pm-1x2",
                    event="pm-evt",
                )
            ],
            policy=_policy(max_profit_give_up_gbp=Decimal("0.20")),
            evaluated_at=NOW,
        )
    )
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert decision.decision_reason == "missing_reverse_quote"
    assert decision.validated_exit_pnl_gbp is None
    assert decision.exit_margin_gbp is None
    assert decision.exit_margin_actionable is False
    assert decision.close_blocker == "missing_reverse_quote"
    assert decision.close_plan.evaluated_at == NOW

    snapshot = snapshot_from_decision(
        decision,
        evaluated_at=NOW,
        auto_unwind_enabled=False,
        auto_close_allowed=True,
        auto_action=PositionManagementAutoAction.ADVISORY_ONLY,
    )
    assert snapshot.evaluated_at == NOW
    assert snapshot.close_blocker == "missing_reverse_quote"
    assert snapshot.exit_margin_gbp is None
    assert snapshot.exit_margin_actionable is False


def test_engine_execution_risk_keeps_recommendation_not_safe_with_known_economics() -> None:
    class HighRisk:
        def score(self, data: ExecutionRiskInputs) -> ExecutionRiskResult:
            del data
            return ExecutionRiskResult(score=95, band="high", reasons=["test_high_risk"])

    engine = PaperUnwindEngine(risk=HighRisk())
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="2.00", size="10")], hold="1.25"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("1.90"), available_stake=Decimal("200"))],
                    cost=_lay_cost(rate="0"),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            policy=_policy(max_profit_give_up_gbp=Decimal("1.00"), max_execution_risk=60),
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    assert decision.validated_exit_pnl_gbp is not None
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert decision.decision_reason == "close_execution_risk_exceeded"
    assert decision.exit_margin_gbp is not None
    assert decision.exit_margin_gbp >= 0
    assert decision.exit_margin_actionable is False
    assert decision.close_blocker == "close_execution_risk_exceeded"


def test_snapshot_exposes_latest_evaluation_timestamp_and_does_not_change_recommendation() -> None:
    engine = PaperUnwindEngine()
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([_open_leg(price="2.00", size="10")], hold="1.25"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    outcome="home",
                    levels=[BookLevel(decimal_odds=Decimal("1.90"), available_stake=Decimal("200"))],
                    cost=_lay_cost(rate="0"),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                )
            ],
            policy=_policy(max_profit_give_up_gbp=Decimal("0.20")),
            evaluated_at=NOW,
        )
    )
    snapshot = snapshot_from_decision(
        decision,
        evaluated_at=NOW,
        auto_unwind_enabled=False,
        auto_close_allowed=True,
        auto_action=PositionManagementAutoAction.ADVISORY_ONLY,
    )
    assert snapshot.evaluated_at == NOW
    assert snapshot.recommendation is decision.recommendation
    assert snapshot.decision_reason == decision.decision_reason
    assert snapshot.exit_margin_gbp == decision.exit_margin_gbp
    assert snapshot.exit_threshold_gbp == decision.exit_threshold_gbp
    assert snapshot.exit_margin_basis is decision.exit_margin_basis
    assert snapshot.paper_only is True
    assert snapshot.places_orders is False
    assert snapshot.spendable is False


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


def _sell_cost() -> VenueCostSnapshot:
    return VenueCostSnapshot(
        venue=VenueName.POLYMARKET,
        action=MarketAction.SELL,
        fee_basis=FeeBasis.NONE_CONFIRMED,
        known_status=CostKnownStatus.KNOWN,
        captured_at=NOW,
        source="test_close_fee",
        order_role=OrderRole.NOT_APPLICABLE,
        fee_scope=FeeScope.PER_QUOTE,
        currency="USD",
        detail="test polymarket closing sell",
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
        settlement_fingerprint_key=FINGERPRINT,
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


def _position(legs: list[OpenPaperLeg], *, hold: str = "10") -> OpenPaperPosition:
    return OpenPaperPosition(
        trade_id="ptrade-exit-margin",
        opportunity_id="opp-demo",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_fingerprint_key=legs[0].settlement_fingerprint_key,
        solver_model="simple_complete_set",
        hold_pnl_gbp=Decimal(hold),
        capital_locked_native={
            leg.native_currency: sum(
                (item.filled_size for item in legs if item.native_currency == leg.native_currency),
                Decimal("0"),
            )
            for leg in legs
        },
        legs=legs,
    )
