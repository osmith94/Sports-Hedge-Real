"""Issue #187: paper position management and exact SELL close fees.

PAPER-ONLY. Data class: modelled / fixture. No venue writes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import _run_paper_position_management
from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.demo_fixtures import tighten_reverse_quotes
from sports_hedge.application.demo_walkthrough import DemoWalkthroughService, FixtureReplayRequest
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
)
from sports_hedge.fees.effective import CostRuleError, apply_closing_action_costs
from sports_hedge.fees.kalshi import (
    KALSHI_QUADRATIC_FORMULA,
    kalshi_closing_cost_from_series,
    kalshi_cost_from_series,
)
from sports_hedge.fees.polymarket import POLYMARKET_TAKER_FORMULA, polymarket_cost_from_market
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.position_management import (
    CompetingOpportunityInput,
    PaperPositionManager,
    build_capital_scarcity,
)
from sports_hedge.paper.position_management.models import PositionManagementAutoAction
from sports_hedge.paper.trades import (
    PAPER_UNWIND_SOURCE,
    PaperLegFillKind,
    PaperSettlementRequest,
    PaperTrade,
    PaperTradeAuditEventType,
    PaperTradeLeg,
    PaperTradeState,
    paper_unwind_source_id,
)
from sports_hedge.paper.unwind import PaperUnwindEngine
from sports_hedge.paper.unwind.models import (
    CapitalPressure,
    CapitalScarcityInput,
    OpenPaperLeg,
    OpenPaperPosition,
    RemainingLockSource,
    ReverseQuote,
    UnwindEvaluationRequest,
    UnwindPolicy,
    UnwindRecommendation,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
FINGERPRINT = "ft:regulation:match_result:v1"
KALSHI_SERIES = {"fee_type": "quadratic", "fee_multiplier": 1}
# Close odds where a 50-share PM BUY @ 2.00 still greens except for taker fees.
FEE_BEARING_PM_CLOSE = [BookLevel(decimal_odds=Decimal("1.60"), available_stake=Decimal("500"))]
AUTO_UNWIND_POLICY = UnwindPolicy(max_profit_give_up_gbp=Decimal("1000"), max_execution_risk=100)


def _eval_at(quotes) -> datetime:
    """Fee snapshots fail closed if evaluation time is before captured_at."""

    times = [quote.quoted_at for quote in quotes if quote.quoted_at is not None]
    return max(times) if times else datetime.now(UTC)


def _refresh_quotes(quotes: list[ReverseQuote], *, delta_ms: int = 1) -> list[ReverseQuote]:
    return [
        item.model_copy(update={"quoted_at": item.quoted_at + timedelta(milliseconds=delta_ms)})
        for item in quotes
    ]


def _second_pass(first: list[ReverseQuote], second: list[ReverseQuote] | None = None):
    refreshed = second if second is not None else _refresh_quotes(first)

    def quotes_for(_position):
        return refreshed

    return quotes_for


def _pm_taker_sell() -> VenueCostSnapshot:
    return polymarket_cost_from_market(
        {
            "id": "pm-fee-on",
            "feesEnabled": True,
            "feeSchedule": {"rate": "0.03", "exponent": 1, "takerOnly": True},
        },
        action=MarketAction.SELL,
        captured_at=NOW,
        source_market_id="pm-fee-on",
    )


def _pm_disabled_sell() -> VenueCostSnapshot:
    return polymarket_cost_from_market(
        {"id": "pm-off", "feesEnabled": False},
        action=MarketAction.SELL,
        captured_at=NOW,
        source_market_id="pm-off",
    )


def _lay_cost() -> VenueCostSnapshot:
    return VenueCostSnapshot.per_quote_profit_commission(
        VenueName.MATCHBOOK,
        Decimal("0.02"),
        action=MarketAction.LAY,
        source="test_close_fee",
        captured_at=NOW,
        currency="GBP",
    )


def _open_leg(
    *,
    venue: VenueName,
    action: MarketAction,
    price: str,
    size: str,
    currency: str,
    runner: str,
    market: str,
    event: str,
    outcome: str = "home",
    fill_kind: PaperLegFillKind = PaperLegFillKind.INTERNAL_SIMULATED,
    fill_id: str | None = "fill-1",
) -> OpenPaperLeg:
    return OpenPaperLeg(
        venue=venue,
        source_event_id=event,
        source_market_id=market,
        source_runner_id=runner,
        canonical_market_id="mkt-1",
        canonical_outcome=outcome,
        canonical_state=outcome,
        opening_action=action,
        filled_price=Decimal(price),
        filled_size=Decimal(size),
        native_currency=currency,
        settlement_fingerprint_key=FINGERPRINT,
        fill_kind=fill_kind,
        fill_id=fill_id,
    )


def _quote(
    *,
    venue: VenueName,
    cost: VenueCostSnapshot,
    currency: str,
    runner: str,
    market: str,
    event: str,
    levels: list[BookLevel] | None = None,
    age_ms: int | None = 50,
    outcome: str = "home",
) -> ReverseQuote:
    return ReverseQuote(
        venue=venue,
        source_event_id=event,
        source_market_id=market,
        source_runner_id=runner,
        canonical_outcome=outcome,
        settlement_fingerprint_key=FINGERPRINT,
        native_currency=currency,
        levels=levels
        or [BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("500"))],
        quote_age_ms=age_ms,
        quote_age_basis="source",
        quoted_at=NOW,
        closing_cost=cost,
    )


def _position(legs: list[OpenPaperLeg], hold: str = "10") -> OpenPaperPosition:
    return OpenPaperPosition(
        trade_id="ptrade-pmgt",
        opportunity_id="opp-pmgt",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_fingerprint_key=FINGERPRINT,
        solver_model="simple_complete_set",
        hold_pnl_gbp=Decimal(hold),
        legs=legs,
    )


def _bundle(tmp_path: Path, *, auto_unwind: bool = False):
    ledger = SqlitePaperLedger(
        tmp_path / "pmgt.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.80"),
        fx_source="paper_demo_fx_snapshot",
    )
    settings = Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=False,
        paper_auto_unwind_enabled=auto_unwind,
        paper_treasury_seed_gbp=1000,
        paper_treasury_demo_usd_gbp_per_unit=0.80,
        paper_treasury_demo_fx_source="paper_demo_fx_snapshot",
    )
    repository = SqliteMarketIntelligenceRepository()
    scan = PaperScanService(MarketIntelligenceService(repository), settings=settings)
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    demo = DemoWalkthroughService(
        operations=ops,
        scan=scan,
        watchlist=watchlist,
        ledger=ledger,
        settings=settings,
    )
    manager = PaperPositionManager(ops, settings=settings)
    return demo, ops, ledger, repository, manager


def test_polymarket_formula_sell_close_matches_taker_formula() -> None:
    snapshot = _pm_taker_sell()
    assert snapshot.action is MarketAction.SELL
    assert snapshot.fee_basis is FeeBasis.FORMULA
    assert snapshot.formula_name == POLYMARKET_TAKER_FORMULA
    # 100 shares at 50¢ → C=100, p=0.5, fee = 100 × 0.03 × 0.25 = 0.75
    priced = apply_closing_action_costs(
        snapshot,
        action=MarketAction.SELL,
        gross_proceeds=Decimal("50"),
        matched_stake=Decimal("100"),
        as_of=NOW,
    )
    assert priced.venue_fee == Decimal("0.75")
    assert priced.net_proceeds == Decimal("49.25")
    assert priced.deferred_profit_commission is False


def test_polymarket_fee_disabled_sell_is_known_zero() -> None:
    snapshot = _pm_disabled_sell()
    assert snapshot.fee_basis is FeeBasis.NONE_CONFIRMED
    priced = apply_closing_action_costs(
        snapshot,
        action=MarketAction.SELL,
        gross_proceeds=Decimal("50"),
        matched_stake=Decimal("100"),
        as_of=NOW,
    )
    assert priced.venue_fee == Decimal("0")


def test_polymarket_missing_formula_sell_fails_closed() -> None:
    unknown = polymarket_cost_from_market(
        {"id": "bare", "feesEnabled": True},
        action=MarketAction.SELL,
        captured_at=NOW,
    )
    assert unknown.known_status is CostKnownStatus.UNKNOWN
    with pytest.raises(CostRuleError, match="unknown"):
        apply_closing_action_costs(
            unknown,
            action=MarketAction.SELL,
            gross_proceeds=Decimal("50"),
            matched_stake=Decimal("100"),
            as_of=NOW,
        )
    unregistered = VenueCostSnapshot(
        venue=VenueName.POLYMARKET,
        action=MarketAction.SELL,
        fee_basis=FeeBasis.FORMULA,
        known_status=CostKnownStatus.KNOWN,
        captured_at=NOW,
        source="test",
        formula_name="unregistered",
        order_role=OrderRole.TAKER,
        fee_scope=FeeScope.PER_QUOTE,
        currency="USD",
    )
    with pytest.raises(CostRuleError, match="FORMULA"):
        apply_closing_action_costs(
            unregistered,
            action=MarketAction.SELL,
            gross_proceeds=Decimal("50"),
            matched_stake=Decimal("100"),
            as_of=NOW,
        )


def test_kalshi_sell_close_uses_quadratic_and_rejects_flat() -> None:
    sell = kalshi_closing_cost_from_series(
        KALSHI_SERIES, captured_at=NOW, source_market_id="KX-1"
    )
    assert sell.action is MarketAction.SELL
    assert sell.fee_basis is FeeBasis.FORMULA
    assert sell.formula_name == KALSHI_QUADRATIC_FORMULA
    buy = kalshi_cost_from_series(KALSHI_SERIES, captured_at=NOW, source_market_id="KX-1")
    assert buy.action is MarketAction.BUY
    priced = apply_closing_action_costs(
        sell,
        action=MarketAction.SELL,
        gross_proceeds=Decimal("50"),
        matched_stake=Decimal("100"),
        as_of=NOW,
    )
    assert priced.venue_fee > 0
    buy_priced = apply_closing_action_costs(
        kalshi_closing_cost_from_series(
            {"fee_type": "quadratic", "fee_multiplier": 0},
            captured_at=NOW,
        ),
        action=MarketAction.SELL,
        gross_proceeds=Decimal("50"),
        matched_stake=Decimal("100"),
        as_of=NOW,
    )
    assert buy_priced.venue_fee == Decimal("0")
    flat = kalshi_closing_cost_from_series(
        {"fee_type": "flat", "fee_multiplier": 1}, captured_at=NOW
    )
    assert flat.known_status is CostKnownStatus.UNKNOWN
    assert flat.action is MarketAction.SELL
    with pytest.raises(CostRuleError):
        apply_closing_action_costs(
            flat,
            action=MarketAction.SELL,
            gross_proceeds=Decimal("50"),
            matched_stake=Decimal("100"),
            as_of=NOW,
        )


def test_matchbook_deferred_commission_is_not_double_charged() -> None:
    engine = PaperUnwindEngine()
    mb = _open_leg(
        venue=VenueName.MATCHBOOK,
        action=MarketAction.BACK,
        price="2.00",
        size="100",
        currency="GBP",
        runner="mb-home",
        market="mb-1x2",
        event="mb-evt",
    )
    decision = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([mb], hold="10"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                    event="mb-evt",
                    levels=[
                        BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("200"))
                    ],
                )
            ],
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    leg = decision.close_plan.legs[0]
    assert leg.deferred_profit_commission is True
    # Gross green P&L is 0 at even close; commission applies only to positive net.
    assert leg.closing_fee == Decimal("0")
    profitable = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([mb], hold="10"),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                    event="mb-evt",
                    levels=[
                        BookLevel(decimal_odds=Decimal("1.50"), available_stake=Decimal("400"))
                    ],
                )
            ],
            evaluated_at=NOW,
        )
    )
    close = profitable.close_plan.legs[0]
    assert close.deferred_profit_commission is True
    gross = close.matched_stake - mb.filled_size
    assert close.closing_fee == max(gross, Decimal("0")) * Decimal("0.02")
    assert close.native_close_pnl == gross - close.closing_fee


def test_abundant_treasury_fee_bearing_close_holds() -> None:
    engine = PaperUnwindEngine()
    pm = _open_leg(
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
            position=_position([pm], hold="10"),
            quotes=[
                _quote(
                    venue=VenueName.POLYMARKET,
                    cost=_pm_taker_sell(),
                    currency="USD",
                    runner="pm-home",
                    market="pm-1x2",
                    event="pm-evt",
                    levels=FEE_BEARING_PM_CLOSE,
                )
            ],
            fx=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW, source="test")],
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("0"), max_execution_risk=100),
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    assert decision.unwind_cost_gbp is not None and decision.unwind_cost_gbp > 0
    assert decision.recommendation is UnwindRecommendation.HOLD
    assert decision.close_plan.legs[0].closing_fee > 0


def test_scarce_explicit_opportunity_cost_makes_give_up_eligible() -> None:
    engine = PaperUnwindEngine()
    pm = _open_leg(
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
            position=_position([pm], hold="10"),
            quotes=[
                _quote(
                    venue=VenueName.POLYMARKET,
                    cost=_pm_taker_sell(),
                    currency="USD",
                    runner="pm-home",
                    market="pm-1x2",
                    event="pm-evt",
                    levels=FEE_BEARING_PM_CLOSE,
                )
            ],
            fx=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW, source="test")],
            scarcity=CapitalScarcityInput(
                pressure=CapitalPressure.SCARCE,
                opportunity_cost_gbp=Decimal("5"),
            ),
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("0"), max_execution_risk=100),
            evaluated_at=NOW,
        )
    )
    assert decision.unwind_cost_gbp is not None
    assert decision.unwind_cost_gbp <= Decimal("5")
    assert decision.recommendation is UnwindRecommendation.UNWIND_ELIGIBLE
    assert decision.decision_reason == "unwind_cost_within_supplied_opportunity_cost"


def test_manager_scarce_opportunity_cost_marks_prediction_sell_eligible(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "scarce-pm.sqlite")
    watchlist = WatchlistService(SqliteWatchlistRepository(tmp_path / "wl.sqlite"))
    settings = Settings(paper_auto_unwind_enabled=False)
    ops = PaperOperationsService(watchlist=watchlist, settings=settings, ledger=ledger)
    trade = PaperTrade(
        trade_id="ptrade-scarce-pm",
        opportunity_id="opp-scarce-pm",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_key=FINGERPRINT,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        guaranteed_profit_gbp_at_open=Decimal("10"),
        legs=[
            PaperTradeLeg(
                venue=VenueName.POLYMARKET,
                outcome="home",
                currency="USD",
                requested_stake=Decimal("50"),
                filled_stake=Decimal("50"),
                displayed_odds=Decimal("2.00"),
                filled_odds=Decimal("2.00"),
                source_market_id="pm-1x2",
                source_event_id="pm-evt",
                source_runner_id="pm-home",
                opening_action=MarketAction.BUY,
                canonical_state="home",
                settlement_fingerprint_key=FINGERPRINT,
                fill_id="pm-fill",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            )
        ],
        fx_snapshots=[
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW, source="test")
        ],
    )
    ledger.trades.save(trade)
    manager = PaperPositionManager(ops, settings=settings)
    result = manager.manage_trade(
        trade.trade_id,
        quotes=[
            _quote(
                venue=VenueName.POLYMARKET,
                cost=_pm_taker_sell(),
                currency="USD",
                runner="pm-home",
                market="pm-1x2",
                event="pm-evt",
                levels=FEE_BEARING_PM_CLOSE,
            )
        ],
        policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("0"), max_execution_risk=100),
        scarcity=CapitalScarcityInput(
            pressure=CapitalPressure.SCARCE,
            opportunity_cost_gbp=Decimal("5"),
        ),
        auto_unwind=False,
        now=NOW,
    )
    assert result.snapshot.recommendation is UnwindRecommendation.UNWIND_ELIGIBLE
    assert result.snapshot.decision_reason == "unwind_cost_within_supplied_opportunity_cost"
    assert result.mutated is False
    ledger.close()


def test_hold_snapshot_carries_modelled_eta_and_unknown_eta(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "eta.sqlite")
    watchlist = WatchlistService(SqliteWatchlistRepository(tmp_path / "wl.sqlite"))
    settings = Settings(paper_auto_unwind_enabled=False)
    ops = PaperOperationsService(watchlist=watchlist, settings=settings, ledger=ledger)
    trade = PaperTrade(
        trade_id="ptrade-eta",
        opportunity_id="opp-eta",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_key=FINGERPRINT,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        guaranteed_profit_gbp_at_open=Decimal("10"),
        legs=[
            PaperTradeLeg(
                venue=VenueName.POLYMARKET,
                outcome="home",
                currency="USD",
                requested_stake=Decimal("50"),
                filled_stake=Decimal("50"),
                displayed_odds=Decimal("2.00"),
                filled_odds=Decimal("2.00"),
                source_market_id="pm-1x2",
                source_event_id="pm-evt",
                source_runner_id="pm-home",
                opening_action=MarketAction.BUY,
                canonical_state="home",
                settlement_fingerprint_key=FINGERPRINT,
                fill_id="pm-fill",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            )
        ],
        fx_snapshots=[
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW, source="test")
        ],
    )
    ledger.trades.save(trade)
    manager = PaperPositionManager(ops, settings=settings)
    quotes = [
        _quote(
            venue=VenueName.POLYMARKET,
            cost=_pm_taker_sell(),
            currency="USD",
            runner="pm-home",
            market="pm-1x2",
            event="pm-evt",
            levels=FEE_BEARING_PM_CLOSE,
        )
    ]
    unknown = manager.manage_trade(
        trade.trade_id,
        quotes=quotes,
        policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("0"), max_execution_risk=100),
        auto_unwind=False,
        now=NOW,
    )
    assert unknown.snapshot.recommendation is UnwindRecommendation.HOLD
    assert unknown.snapshot.hold_pnl_gbp == Decimal("10")
    assert unknown.snapshot.validated_exit_pnl_gbp is not None
    assert unknown.snapshot.unwind_cost_gbp is not None and unknown.snapshot.unwind_cost_gbp > 0
    assert unknown.snapshot.remaining_lock_basis is RemainingLockSource.UNKNOWN
    assert unknown.snapshot.remaining_lock_minutes is None
    assert unknown.snapshot.normal_release_context == "after authoritative settlement"
    assert unknown.snapshot.remaining_lock_advisory is True
    assert unknown.snapshot.spendable is False
    ops._plans[trade.opportunity_id] = SimpleNamespace(
        decision=SimpleNamespace(
            allocation=SimpleNamespace(
                expected_lock_duration_hours=Decimal("1.5"),
                expected_lock_basis="modelled remaining elapsed",
                estimated_time_to_release=SimpleNamespace(confidence=Decimal("0.40")),
            )
        )
    )
    modelled = manager.manage_trade(
        trade.trade_id,
        quotes=quotes,
        policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("0"), max_execution_risk=100),
        auto_unwind=False,
        now=NOW,
    )
    assert modelled.snapshot.recommendation is UnwindRecommendation.HOLD
    assert modelled.snapshot.remaining_lock_basis is RemainingLockSource.MODELLED
    assert modelled.snapshot.remaining_lock_minutes == Decimal("90")
    assert modelled.snapshot.remaining_lock_confidence == Decimal("0.40")
    assert modelled.snapshot.remaining_lock_advisory is True
    assert modelled.snapshot.normal_release_context == "after authoritative settlement"
    assert modelled.snapshot.spendable is False
    ledger.close()


def test_insufficient_depth_stale_unknown_fx_and_partial_leg_fail_closed() -> None:
    engine = PaperUnwindEngine()
    mb = _open_leg(
        venue=VenueName.MATCHBOOK,
        action=MarketAction.BACK,
        price="2.00",
        size="100",
        currency="GBP",
        runner="mb-home",
        market="mb-1x2",
        event="mb-evt",
        fill_id="mb-fill",
    )
    pm = _open_leg(
        venue=VenueName.POLYMARKET,
        action=MarketAction.BUY,
        price="2.00",
        size="50",
        currency="USD",
        runner="pm-home",
        market="pm-1x2",
        event="pm-evt",
        fill_id="pm-fill",
    )
    thin = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([mb]),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                    event="mb-evt",
                    levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("1"))],
                )
            ],
            evaluated_at=NOW,
        )
    )
    assert thin.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "insufficient_reverse_depth" in thin.close_plan.rejection_reasons
    stale = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([mb]),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                    event="mb-evt",
                    age_ms=5000,
                )
            ],
            policy=UnwindPolicy(max_quote_age_ms=2000),
            evaluated_at=NOW,
        )
    )
    assert stale.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "stale_quote" in stale.close_plan.rejection_reasons
    unknown_age = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([mb]),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                    event="mb-evt",
                    age_ms=None,
                )
            ],
            evaluated_at=NOW,
        )
    )
    assert unknown_age.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "unknown_quote_age" in unknown_age.close_plan.rejection_reasons
    missing_fx = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([pm]),
            quotes=[
                _quote(
                    venue=VenueName.POLYMARKET,
                    cost=_pm_disabled_sell(),
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
    assert missing_fx.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert any(reason.startswith("missing_fx_rate") for reason in missing_fx.close_plan.rejection_reasons)
    unknown_fee = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([pm]),
            quotes=[
                _quote(
                    venue=VenueName.POLYMARKET,
                    cost=VenueCostSnapshot(
                        venue=VenueName.POLYMARKET,
                        action=MarketAction.SELL,
                        fee_basis=FeeBasis.UNKNOWN,
                        known_status=CostKnownStatus.UNKNOWN,
                        captured_at=NOW,
                        source="test",
                        currency="USD",
                    ),
                    currency="USD",
                    runner="pm-home",
                    market="pm-1x2",
                    event="pm-evt",
                )
            ],
            fx=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW, source="test")],
            evaluated_at=NOW,
        )
    )
    assert unknown_fee.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    one_leg = engine.evaluate(
        UnwindEvaluationRequest(
            position=_position([mb, pm]),
            quotes=[
                _quote(
                    venue=VenueName.MATCHBOOK,
                    cost=_lay_cost(),
                    currency="GBP",
                    runner="mb-home",
                    market="mb-1x2",
                    event="mb-evt",
                )
            ],
            fx=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW, source="test")],
            evaluated_at=NOW,
        )
    )
    assert one_leg.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "missing_reverse_quote" in one_leg.close_plan.rejection_reasons
    assert one_leg.conditionally_releasable_by_venue_currency == {}


def test_auto_unwind_second_revalidation_abort_and_no_treasury_mutation(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        good = tighten_reverse_quotes(opened.quotes)
        bad = [
            quote.model_copy(
                update={
                    "quoted_at": quote.quoted_at + timedelta(milliseconds=1),
                    "levels": [
                        BookLevel(decimal_odds=Decimal("8.00"), available_stake=Decimal("1"))
                    ],
                }
            )
            for quote in good
        ]
        calls = {"n": 0}

        def second_quotes_for(_position):
            calls["n"] += 1
            return bad

        before = {
            (pool.venue, pool.native_currency): (pool.available_cash, pool.locked_capital)
            for pool in ledger.treasury.snapshot().pools
        }
        result = manager.manage_trade(
            trade.trade_id,
            quotes=good,
            second_quotes_for=second_quotes_for,
            policy=AUTO_UNWIND_POLICY,
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            auto_unwind=True,
            now=_eval_at(good),
        )
        assert calls["n"] == 1
        assert "second_revalidation" in result.aborted_reason
        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        assert persisted.close_fills == []
        after = {
            (pool.venue, pool.native_currency): (pool.available_cash, pool.locked_capital)
            for pool in ledger.treasury.snapshot().pools
        }
        assert after == before
        assert any(
            event.event_type is PaperTradeAuditEventType.UNWIND_ABORTED for event in persisted.audit
        )
    finally:
        repository.close()
        ledger.close()


def test_same_timestamp_second_pass_cannot_auto_close(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        before = {
            (pool.venue, pool.native_currency): (pool.available_cash, pool.locked_capital)
            for pool in ledger.treasury.snapshot().pools
        }

        def second_quotes_for(_position):
            return quotes

        result = manager.manage_trade(
            trade.trade_id,
            quotes=quotes,
            second_quotes_for=second_quotes_for,
            policy=AUTO_UNWIND_POLICY,
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            auto_unwind=True,
            now=_eval_at(quotes),
        )
        assert result.mutated is False
        assert result.aborted_reason == "second_revalidation_not_fresh"
        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        assert persisted.close_fills == []
        after = {
            (pool.venue, pool.native_currency): (pool.available_cash, pool.locked_capital)
            for pool in ledger.treasury.snapshot().pools
        }
        assert after == before
    finally:
        repository.close()
        ledger.close()


def _treasury_fingerprint(ledger: SqlitePaperLedger) -> dict:
    return {
        (pool.venue, pool.native_currency): (
            pool.available_cash,
            pool.locked_capital,
            pool.realised_pnl_native,
        )
        for pool in ledger.treasury.snapshot().pools
    }


def _collection_report() -> CollectionReport:
    return CollectionReport(started_at=NOW, completed_at=NOW)


def test_first_eligible_cycle_pends_without_closing(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        before = _treasury_fingerprint(ledger)
        result = manager.manage_trade(
            trade.trade_id,
            quotes=quotes,
            policy=AUTO_UNWIND_POLICY,
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            auto_unwind=True,
            now=_eval_at(quotes),
        )
        assert result.mutated is False
        assert result.aborted_reason is None
        assert result.snapshot.auto_action is PositionManagementAutoAction.UNWIND_PENDING_CONFIRMATION
        assert result.snapshot.pending_confirmation is not None
        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        assert persisted.close_fills == []
        assert _treasury_fingerprint(ledger) == before
    finally:
        repository.close()
        ledger.close()


def test_first_pass_quotes_for_is_not_reused_for_auto_close(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        calls = {"n": 0}

        def quotes_for(_position):
            calls["n"] += 1
            return _refresh_quotes(quotes, delta_ms=calls["n"] * 5)

        before = _treasury_fingerprint(ledger)
        result = manager.manage_trade(
            trade.trade_id,
            quotes_for=quotes_for,
            policy=AUTO_UNWIND_POLICY,
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            auto_unwind=True,
            now=_eval_at(quotes),
        )
        assert calls["n"] == 1
        assert result.mutated is False
        assert result.aborted_reason is None
        assert result.snapshot.auto_action is PositionManagementAutoAction.UNWIND_PENDING_CONFIRMATION
        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        assert persisted.close_fills == []
        assert _treasury_fingerprint(ledger) == before
    finally:
        repository.close()
        ledger.close()


def test_production_manage_open_positions_first_cycle_does_not_auto_close(
    tmp_path: Path,
) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        before = _treasury_fingerprint(ledger)
        results = manager.manage_open_positions(
            quotes_by_trade={trade.trade_id: quotes},
            policy=AUTO_UNWIND_POLICY,
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            auto_unwind=True,
            now=_eval_at(quotes),
        )
        assert len(results) == 1
        assert results[0].mutated is False
        assert results[0].snapshot.auto_action is PositionManagementAutoAction.UNWIND_PENDING_CONFIRMATION
        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        assert persisted.close_fills == []
        assert _treasury_fingerprint(ledger) == before
    finally:
        repository.close()
        ledger.close()


def test_scheduled_two_scan_closes_once_on_newer_qualifying_cycle(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        before = _treasury_fingerprint(ledger)
        first = _run_paper_position_management(
            _collection_report(),
            operations=ops,
            manager=manager,
            policy=AUTO_UNWIND_POLICY,
            now=_eval_at(quotes),
            quotes_by_trade={trade.trade_id: quotes},
        )
        assert len(first) == 1
        assert first[0].mutated is False
        assert first[0].snapshot.auto_action is PositionManagementAutoAction.UNWIND_PENDING_CONFIRMATION
        pending = ops.trades.get(trade.trade_id)
        assert pending is not None
        assert pending.state is PaperTradeState.OPEN
        assert pending.close_fills == []
        assert _treasury_fingerprint(ledger) == before

        same_scan = _run_paper_position_management(
            _collection_report(),
            operations=ops,
            manager=manager,
            policy=AUTO_UNWIND_POLICY,
            now=_eval_at(quotes),
            quotes_by_trade={trade.trade_id: quotes},
        )
        assert same_scan[0].mutated is False
        still_open = ops.trades.get(trade.trade_id)
        assert still_open is not None
        assert still_open.state is PaperTradeState.OPEN
        assert still_open.position_management is not None
        assert (
            still_open.position_management.auto_action
            is PositionManagementAutoAction.UNWIND_PENDING_CONFIRMATION
        )
        assert _treasury_fingerprint(ledger) == before

        newer = _refresh_quotes(quotes)
        closed_cycle = _run_paper_position_management(
            _collection_report(),
            operations=ops,
            manager=manager,
            policy=AUTO_UNWIND_POLICY,
            now=_eval_at(newer),
            quotes_by_trade={trade.trade_id: newer},
        )
        assert closed_cycle[0].mutated is True
        closed = ops.trades.get(trade.trade_id)
        assert closed is not None
        assert closed.state is PaperTradeState.CLOSED
        assert closed.settlement_source == PAPER_UNWIND_SOURCE
        assert closed.settlement_source_id == paper_unwind_source_id(trade.trade_id)
        assert closed.close_fills
        after_close = _treasury_fingerprint(ledger)
        assert after_close != before

        unwind_ids = [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        assert unwind_ids
        third = _run_paper_position_management(
            _collection_report(),
            operations=ops,
            manager=manager,
            policy=AUTO_UNWIND_POLICY,
            now=_eval_at(_refresh_quotes(newer, delta_ms=2)),
            quotes_by_trade={trade.trade_id: _refresh_quotes(newer, delta_ms=2)},
        )
        assert third == []
        again = ops.trades.get(trade.trade_id)
        assert again is not None
        assert again.state is PaperTradeState.CLOSED
        assert [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ] == unwind_ids
        assert _treasury_fingerprint(ledger) == after_close
    finally:
        repository.close()
        ledger.close()


def test_scheduled_two_scan_non_eligible_newer_cycle_stays_open(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        before = _treasury_fingerprint(ledger)
        _run_paper_position_management(
            _collection_report(),
            operations=ops,
            manager=manager,
            policy=AUTO_UNWIND_POLICY,
            now=_eval_at(quotes),
            quotes_by_trade={trade.trade_id: quotes},
        )
        bad = [
            quote.model_copy(
                update={
                    "quoted_at": quote.quoted_at + timedelta(milliseconds=1),
                    "levels": [
                        BookLevel(decimal_odds=Decimal("8.00"), available_stake=Decimal("1"))
                    ],
                }
            )
            for quote in quotes
        ]
        later = _run_paper_position_management(
            _collection_report(),
            operations=ops,
            manager=manager,
            policy=AUTO_UNWIND_POLICY,
            now=_eval_at(bad),
            quotes_by_trade={trade.trade_id: bad},
        )
        assert later[0].mutated is False
        assert later[0].aborted_reason is not None
        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        assert persisted.close_fills == []
        assert persisted.position_management is not None
        assert persisted.position_management.pending_confirmation is None
        assert persisted.position_management.auto_action is PositionManagementAutoAction.UNWIND_ABORTED
        assert _treasury_fingerprint(ledger) == before
    finally:
        repository.close()
        ledger.close()


def test_scheduled_two_scan_changed_eligible_facts_stay_open(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        before = _treasury_fingerprint(ledger)
        _run_paper_position_management(
            _collection_report(),
            operations=ops,
            manager=manager,
            policy=AUTO_UNWIND_POLICY,
            now=_eval_at(quotes),
            quotes_by_trade={trade.trade_id: quotes},
        )
        shifted = [
            quote.model_copy(
                update={
                    "quoted_at": quote.quoted_at + timedelta(milliseconds=1),
                    "levels": [
                        level.model_copy(
                            update={
                                "decimal_odds": max(
                                    Decimal("1.01"),
                                    level.decimal_odds - Decimal("0.01"),
                                )
                            }
                        )
                        for level in quote.levels
                    ],
                }
            )
            for quote in quotes
        ]
        later = _run_paper_position_management(
            _collection_report(),
            operations=ops,
            manager=manager,
            policy=AUTO_UNWIND_POLICY,
            now=_eval_at(shifted),
            quotes_by_trade={trade.trade_id: shifted},
        )
        assert later[0].mutated is False
        assert later[0].aborted_reason == "second_revalidation_facts_changed"
        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        assert persisted.close_fills == []
        assert persisted.position_management is not None
        assert persisted.position_management.pending_confirmation is not None
        assert (
            persisted.position_management.auto_action
            is PositionManagementAutoAction.UNWIND_PENDING_CONFIRMATION
        )
        assert _treasury_fingerprint(ledger) == before
    finally:
        repository.close()
        ledger.close()


def test_auto_unwind_releases_once_and_survives_restart(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        policy = AUTO_UNWIND_POLICY
        scarcity = CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT)
        first = manager.manage_trade(
            trade.trade_id,
            quotes=quotes,
            second_quotes_for=_second_pass(quotes),
            policy=policy,
            scarcity=scarcity,
            auto_unwind=True,
            now=_eval_at(quotes),
        )
        assert first.mutated is True
        closed = ops.trades.get(trade.trade_id)
        assert closed is not None
        assert closed.state is PaperTradeState.CLOSED
        assert closed.settlement_source == PAPER_UNWIND_SOURCE
        assert closed.settlement_source_id == paper_unwind_source_id(trade.trade_id)
        assert closed.close_fills
        assert closed.realised_pnl_gbp is not None
        unwind_ids = [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        assert unwind_ids
        assert all(
            item.startswith(paper_unwind_source_id(trade.trade_id)) for item in unwind_ids
        )
        fingerprint = {
            (pool.venue, pool.native_currency): (
                pool.available_cash,
                pool.locked_capital,
                pool.realised_pnl_native,
            )
            for pool in ledger.treasury.snapshot().pools
        }
        second = manager.manage_trade(
            trade.trade_id,
            quotes=quotes,
            second_quotes_for=_second_pass(quotes),
            policy=policy,
            scarcity=scarcity,
            auto_unwind=True,
            now=_eval_at(quotes),
        )
        assert second.mutated is False
        again = ops.complete_validated_unwind(
            trade.trade_id, quotes=quotes, policy=policy, scarcity=scarcity, now=_eval_at(quotes)
        )
        assert again.state is PaperTradeState.CLOSED
        assert [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ] == unwind_ids
        path = tmp_path / "pmgt.sqlite"
        ledger.close()
        reopened = SqlitePaperLedger(
            path,
            seed_gbp=Decimal("1000"),
            usd_gbp_per_unit=Decimal("0.80"),
            fx_source="paper_demo_fx_snapshot",
            auto_seed=False,
        )
        try:
            loaded = reopened.trades.get(trade.trade_id)
            assert loaded is not None
            assert loaded.state is PaperTradeState.CLOSED
            assert loaded.close_fills
            assert loaded.realised_pnl_gbp == closed.realised_pnl_gbp
            assert loaded.position_management is not None
            restarted = {
                (pool.venue, pool.native_currency): (
                    pool.available_cash,
                    pool.locked_capital,
                    pool.realised_pnl_native,
                )
                for pool in reopened.treasury.snapshot().pools
            }
            assert restarted == fingerprint
        finally:
            reopened.close()
    finally:
        repository.close()


def test_concurrent_cycles_produce_one_unwind(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        policy = AUTO_UNWIND_POLICY
        scarcity = CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT)

        def run() -> str:
            result = manager.manage_trade(
                trade.trade_id,
                quotes=quotes,
                second_quotes_for=_second_pass(quotes),
                policy=policy,
                scarcity=scarcity,
                auto_unwind=True,
                now=_eval_at(quotes),
            )
            return "mutated" if result.mutated else (result.aborted_reason or "noop")

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: run(), range(2)))
        closed = ops.trades.get(trade.trade_id)
        assert closed is not None
        assert closed.state is PaperTradeState.CLOSED
        assert closed.settlement_source_id == paper_unwind_source_id(trade.trade_id)
        assert len(closed.close_fills) == len([leg for leg in closed.legs if leg.filled_stake > 0])
        unwind_ids = {
            paper_unwind_source_id(trade.trade_id)
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
            and entry.source_id.startswith(paper_unwind_source_id(trade.trade_id))
        }
        assert unwind_ids == {paper_unwind_source_id(trade.trade_id)}
        assert outcomes.count("mutated") <= 1
    finally:
        repository.close()
        ledger.close()


def test_unwind_then_settlement_remain_mutually_exclusive(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        manager.manage_trade(
            trade.trade_id,
            quotes=quotes,
            second_quotes_for=_second_pass(quotes),
            policy=AUTO_UNWIND_POLICY,
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            auto_unwind=True,
            now=_eval_at(quotes),
        )
        with pytest.raises(PaperOperationsError, match="already_unwound"):
            ops.settle(
                trade.trade_id,
                PaperSettlementRequest(
                    winning_outcome=trade.legs[0].outcome,
                    source="fixture_test",
                    source_id="must-not-settle-unwound",
                ),
            )
    finally:
        repository.close()
        ledger.close()


def test_manual_external_is_advisory_only(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "manual.sqlite")
    watchlist = WatchlistService(SqliteWatchlistRepository(tmp_path / "wl.sqlite"))
    settings = Settings(paper_auto_unwind_enabled=True)
    ops = PaperOperationsService(watchlist=watchlist, settings=settings, ledger=ledger)
    trade = PaperTrade(
        trade_id="ptrade-manual",
        opportunity_id="opp-manual",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_key=FINGERPRINT,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        guaranteed_profit_gbp_at_open=Decimal("8"),
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
                fill_id="mb-fill",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
            PaperTradeLeg(
                venue=VenueName.POLYMARKET,
                outcome="home",
                currency="USD",
                requested_stake=Decimal("50"),
                filled_stake=Decimal("50"),
                displayed_odds=Decimal("2.00"),
                filled_odds=Decimal("2.00"),
                source_market_id="pm-1x2",
                source_event_id="pm-evt",
                source_runner_id="pm-home",
                opening_action=MarketAction.BUY,
                canonical_state="home",
                settlement_fingerprint_key=FINGERPRINT,
                fill_id="pm-fill",
                fill_kind=PaperLegFillKind.MANUAL_EXTERNAL,
            ),
        ],
        fx_snapshots=[
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW, source="test")
        ],
    )
    ledger.trades.save(trade)
    manager = PaperPositionManager(ops, settings=settings)
    quotes = [
        _quote(
            venue=VenueName.MATCHBOOK,
            cost=_lay_cost(),
            currency="GBP",
            runner="mb-home",
            market="mb-1x2",
            event="mb-evt",
        ),
        _quote(
            venue=VenueName.POLYMARKET,
            cost=_pm_disabled_sell(),
            currency="USD",
            runner="pm-home",
            market="pm-1x2",
            event="pm-evt",
        ),
    ]
    result = manager.manage_trade(
        trade.trade_id,
        quotes=quotes,
        policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("1000"), max_execution_risk=100),
        auto_unwind=True,
        now=NOW,
    )
    persisted = ops.trades.get(trade.trade_id)
    assert persisted is not None
    assert persisted.state is PaperTradeState.OPEN
    assert persisted.close_fills == []
    assert result.mutated is False
    assert result.snapshot.auto_action is PositionManagementAutoAction.SKIPPED_MANUAL_EXTERNAL
    ledger.close()


def test_awaiting_manual_external_is_excluded_from_auto_close(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "await.sqlite")
    watchlist = WatchlistService(SqliteWatchlistRepository(tmp_path / "wl.sqlite"))
    settings = Settings(paper_auto_unwind_enabled=True)
    ops = PaperOperationsService(watchlist=watchlist, settings=settings, ledger=ledger)
    trade = PaperTrade(
        trade_id="ptrade-await",
        opportunity_id="opp-await",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_key=FINGERPRINT,
        state=PaperTradeState.AWAITING_MANUAL_EXTERNAL,
        opened_at=NOW,
        last_updated_at=NOW,
        guaranteed_profit_gbp_at_open=Decimal("8"),
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
                fill_id="mb-fill",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            )
        ],
    )
    ledger.trades.save(trade)
    manager = PaperPositionManager(ops, settings=settings)
    result = manager.manage_trade(
        trade.trade_id,
        quotes=[
            _quote(
                venue=VenueName.MATCHBOOK,
                cost=_lay_cost(),
                currency="GBP",
                runner="mb-home",
                market="mb-1x2",
                event="mb-evt",
            )
        ],
        auto_unwind=True,
        now=NOW,
    )
    persisted = ops.trades.get(trade.trade_id)
    assert persisted is not None
    assert persisted.state is PaperTradeState.AWAITING_MANUAL_EXTERNAL
    assert result.snapshot.auto_action is PositionManagementAutoAction.SKIPPED_AWAITING_MANUAL_EXTERNAL
    ledger.close()


def test_repeat_evaluation_does_not_spam_audit(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        quotes = opened.quotes
        policy = UnwindPolicy(max_profit_give_up_gbp=Decimal("0"), max_execution_risk=100)
        manager.manage_trade(trade.trade_id, quotes=quotes, policy=policy, auto_unwind=False, now=NOW)
        first = ops.trades.get(trade.trade_id)
        assert first is not None
        first_count = sum(
            1
            for event in first.audit
            if event.event_type is PaperTradeAuditEventType.POSITION_MANAGEMENT_CHANGED
        )
        manager.manage_trade(trade.trade_id, quotes=quotes, policy=policy, auto_unwind=False, now=NOW)
        second = ops.trades.get(trade.trade_id)
        assert second is not None
        second_count = sum(
            1
            for event in second.audit
            if event.event_type is PaperTradeAuditEventType.POSITION_MANAGEMENT_CHANGED
        )
        assert second_count == first_count
        assert second.position_management is not None
    finally:
        repository.close()
        ledger.close()


def test_scarcity_builder_requires_traceable_opportunity_cost() -> None:
    abundant = build_capital_scarcity()
    assert abundant.pressure is CapitalPressure.ABUNDANT
    assert abundant.opportunity_cost_gbp is None
    scarce = build_capital_scarcity(
        competing=[
            CompetingOpportunityInput(
                opportunity_id="opp-other",
                expected_guaranteed_profit_gbp=Decimal("4.50"),
                allocator_accepted=True,
                required_native={"matchbook:GBP": Decimal("100")},
            )
        ],
        locked_venue_keys={"matchbook:GBP"},
    )
    assert scarce.pressure is CapitalPressure.SCARCE
    assert scarce.opportunity_cost_gbp == Decimal("4.50")


def test_paper_only_boundary_and_feature_gate_default() -> None:
    settings = Settings()
    assert settings.paper_auto_unwind_enabled is False
    assert settings.sports_hedge_execution_enabled is False
    for venue_cls in (MatchbookClient, PolymarketClient, KalshiClient):
        assert not hasattr(venue_cls, "place_order")
        assert not hasattr(venue_cls, "cancel_order")
        assert not hasattr(venue_cls, "sign")
    from sports_hedge.paper.position_management import manager as manager_mod

    source = Path(manager_mod.__file__).read_text(encoding="utf-8")
    for banned in ("place_order", "cancel_order", "sign_wallet", "submit_order"):
        assert banned not in source
    health = TestClient(app).get("/health").json()
    assert health["execution_enabled"] is False
    assert health["paper_auto_unwind_enabled"] is False
    start_ps1 = Path(__file__).resolve().parents[2] / "scripts" / "windows" / "Start-SportsHedge-Demo.ps1"
    assert start_ps1.is_file()
    launcher = start_ps1.read_text(encoding="utf-8")
    assert '$env:PAPER_AUTO_UNWIND_ENABLED = "true"' in launcher
    assert "AUTO PAPER POSITION MANAGEMENT ON" in launcher
    assert "two-scan fail-closed" in launcher
    assert "no live execution" in launcher
    assert "no automatic authoritative settlement" in launcher
    assert '$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"' in launcher
    assert "place_order" not in launcher
    assert "cancel_order" not in launcher


def test_position_management_api_seam(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        manager.manage_trade(
            trade.trade_id,
            quotes=opened.quotes,
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("0"), max_execution_risk=100),
            auto_unwind=False,
            now=NOW,
        )
        from sports_hedge.api.paper import get_paper_operations_service

        app.dependency_overrides[get_paper_operations_service] = lambda: ops
        client = TestClient(app)
        listed = client.get("/paper/trades/position-management")
        assert listed.status_code == 200
        body = listed.json()
        assert body
        assert body[0]["trade_id"] == trade.trade_id
        assert body[0]["paper_only"] is True
        assert body[0]["places_orders"] is False
        assert body[0]["spendable"] is False
        assert body[0]["normal_release_context"] == "after authoritative settlement"
        assert body[0]["remaining_lock_advisory"] is True
        assert "remaining_lock_basis" in body[0]
        one = client.get(f"/paper/trades/{trade.trade_id}/position-management")
        assert one.status_code == 200
        assert one.json()["recommendation"] in {"HOLD", "UNWIND_ELIGIBLE", "UNWIND_NOT_SAFE"}
        active = client.get("/paper/trades/active").json()
        assert active[0]["position_management"]["decision_reason"]
    finally:
        app.dependency_overrides.clear()
        repository.close()
        ledger.close()


def test_simulated_external_may_be_auto_managed(tmp_path: Path) -> None:
    demo, ops, ledger, repository, manager = _bundle(tmp_path, auto_unwind=True)
    try:
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        kinds = {leg.fill_kind for leg in trade.legs}
        assert PaperLegFillKind.PAPER_SIMULATED_EXTERNAL in kinds
        result = manager.manage_trade(
            trade.trade_id,
            quotes=tighten_reverse_quotes(opened.quotes),
            second_quotes_for=_second_pass(tighten_reverse_quotes(opened.quotes)),
            policy=AUTO_UNWIND_POLICY,
            scarcity=CapitalScarcityInput(pressure=CapitalPressure.ABUNDANT),
            auto_unwind=True,
            now=_eval_at(tighten_reverse_quotes(opened.quotes)),
        )
        assert result.snapshot.auto_close_allowed is True
        closed = ops.trades.get(trade.trade_id)
        assert closed is not None
        assert closed.state is PaperTradeState.CLOSED
    finally:
        repository.close()
        ledger.close()
