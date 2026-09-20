from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_ledger, get_paper_operations_service
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.complete_set import SOLVER_MODEL_GENERALIZED, SOLVER_MODEL_SIMPLE
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.models import LegExecutionMode
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.liquidity import PaperLiquiditySnapshot, default_pools
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.trades import PaperLegFillKind, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues import MatchbookClient, PolymarketClient, KalshiClient
from test_kalshi_k1 import KALSHI_EVENT, KALSHI_SERIES, _btts_market
from test_paper_scan_pipeline import KICKOFF, matchbook_payloads, polymarket_payloads
from test_step7_safe_market_expansion import MB_EVENT
from test_step8b_first_team_to_score import _ftts_mb_payload
from venue_cost_helpers import profit_commission_cost

OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
FX = [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), spread_bps=Decimal("0"), source="test")]


def _standing_locked_matchbook() -> PaperLiquiditySnapshot:
    pools = default_pools(
        matchbook_gbp=Decimal("700"),
        polymarket_usd=Decimal("5000"),
        kalshi_usd=Decimal("5000"),
    )
    updated = []
    for pool in pools:
        if pool.venue is VenueName.MATCHBOOK:
            updated.append(pool.model_copy(update={"available": Decimal("700"), "locked": Decimal("300")}))
        else:
            updated.append(pool)
    return PaperLiquiditySnapshot(pools=updated, updated_at=OBSERVED)


def _standing(
    *,
    matchbook_gbp: Decimal = Decimal("5000"),
    polymarket_usd: Decimal = Decimal("5000"),
    kalshi_usd: Decimal = Decimal("5000"),
) -> PaperLiquiditySnapshot:
    return PaperLiquiditySnapshot(
        pools=default_pools(
            matchbook_gbp=matchbook_gbp,
            polymarket_usd=polymarket_usd,
            kalshi_usd=kalshi_usd,
        ),
        updated_at=OBSERVED,
    )


def _settings(*, autofill: bool) -> Settings:
    return Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=autofill,
    )


def _ops_bundle(tmp_path: Path, *, autofill: bool):
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    settings = _settings(autofill=autofill)
    scan = PaperScanService(intelligence, settings=settings)
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    return scan, watchlist, ops, repository, ledger


def _observe_and_persist(
    scan,
    watchlist,
    ops,
    left,
    right,
    *,
    venue_costs,
    extra=None,
    provenance=DataProvenance.LIVE_PAPER,
):
    kwargs = dict(
        venue_costs=venue_costs,
        fx_snapshots=FX,
        maximum_execution_risk=100,
        liquidity_snapshot=_standing(),
    )
    if extra:
        kwargs.update(extra)
    decision = scan.scan_pair(left, right, **kwargs)
    assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
    assert decision.allocation is not None and decision.allocation.accepted
    watchlist.observe_paper_decision(
        decision,
        scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
    )
    ops.persist_triggered_chain(decision, provenance=provenance)
    return decision


def _matchbook_btts():
    event, market = matchbook_payloads()
    return MatchbookObservationBuilder().build(event, market, observed_at=OBSERVED, quote_age_ms=120)


def _polymarket_btts():
    event, market, books = polymarket_payloads()
    return PolymarketObservationBuilder().build(
        event, market, books, observed_at=OBSERVED, quote_age_ms=180
    )


def _kalshi_btts(*, yes_price: str = "0.20", no_price: str = "0.70", size: str = "500.00"):
    market = _btts_market()
    book = {
        "orderbook_fp": {
            "yes_dollars": [[yes_price, size]],
            "no_dollars": [[no_price, size]],
        }
    }
    return KalshiObservationBuilder().build(
        KALSHI_EVENT,
        market,
        {market["ticker"]: book},
        series=KALSHI_SERIES,
        observed_at=OBSERVED,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )


def _kalshi_costs(captured: datetime | None = None):
    captured = captured or datetime.now(UTC)
    return [
        profit_commission_cost(VenueName.MATCHBOOK, "0.02", captured_at=captured),
        kalshi_cost_from_series(KALSHI_SERIES, captured_at=captured),
    ]


def _pm_kalshi_costs(captured: datetime | None = None):
    captured = captured or datetime.now(UTC)
    return [
        profit_commission_cost(VenueName.POLYMARKET, "0", captured_at=captured),
        kalshi_cost_from_series(KALSHI_SERIES, captured_at=captured),
    ]


def _kalshi_ftts_observation():
    from test_step7_safe_market_expansion import KICKOFF as FTTS_KICKOFF

    event = {
        "event_ticker": "KXEPLFTTS-26SEP20TOTEVE",
        "series_ticker": "KXEPLFTTS",
        "title": "Tottenham vs Everton",
        "category": "Sports",
        "strike_date": FTTS_KICKOFF.isoformat(),
    }
    series = {
        "ticker": "KXEPLFTTS",
        "title": "Premier League First Team To Score",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    }
    markets = [
        {
            "ticker": "KXEPLFTTS-26SEP20TOTEVE-TOT",
            "event_ticker": event["event_ticker"],
            "title": "First team to score",
            "yes_sub_title": "Tottenham",
            "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
        },
        {
            "ticker": "KXEPLFTTS-26SEP20TOTEVE-EVE",
            "event_ticker": event["event_ticker"],
            "title": "First team to score",
            "yes_sub_title": "Everton",
            "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
        },
        {
            "ticker": "KXEPLFTTS-26SEP20TOTEVE-NG",
            "event_ticker": event["event_ticker"],
            "title": "First team to score",
            "yes_sub_title": "No Goal",
            "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
        },
    ]
    book = {
        "orderbook_fp": {
            "yes_dollars": [["0.22", "1000.00"]],
            "no_dollars": [["0.75", "1000.00"]],
        }
    }
    return KalshiObservationBuilder().build(
        event,
        markets,
        {item["ticker"]: dict(book) for item in markets},
        series=series,
        observed_at=OBSERVED,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )


def _ftts_kalshi_costs(captured: datetime | None = None):
    captured = captured or datetime.now(UTC)
    return [
        profit_commission_cost(VenueName.MATCHBOOK, "0.02", captured_at=captured),
        kalshi_cost_from_series(
            {
                "ticker": "KXEPLFTTS",
                "title": "Premier League First Team To Score",
                "fee_type": "quadratic",
                "fee_multiplier": 1,
            },
            captured_at=captured,
        ),
    ]


def _assert_allocator_sized(ops, trade) -> None:
    allocation = ops._plans[trade.opportunity_id].decision.allocation
    assert allocation is not None and allocation.accepted
    positive = [stake for stake in allocation.recommended_stakes if stake.stake_native > 0]
    assert len(trade.legs) == len(positive)
    for stake in positive:
        match = next(
            leg
            for leg in trade.legs
            if leg.venue is stake.venue
            and leg.outcome == stake.outcome
            and leg.source_market_id == stake.source_market_id
        )
        assert match.requested_stake == stake.stake_native
        assert match.filled_stake == stake.stake_native
        assert match.filled_stake > 0


def test_simple_matchbook_polymarket_autofill_uses_allocator_size(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        before = ledger.treasury.snapshot()
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        assert decision.solver_model == SOLVER_MODEL_SIMPLE
        active = ops.list_active_trades()
        assert len(active) == 1
        trade = active[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.places_orders is False
        assert trade.paper_only is True
        kinds = {leg.fill_kind for leg in trade.legs}
        assert kinds == {PaperLegFillKind.INTERNAL_SIMULATED}
        assert PaperLegFillKind.MANUAL_EXTERNAL not in kinds
        _assert_allocator_sized(ops, trade)
        assert trade.guaranteed_profit_gbp_at_open == decision.allocation.guaranteed_profit
        after = ledger.treasury.snapshot()
        mb = after.pool(VenueName.MATCHBOOK, "GBP")
        ks = after.pool(VenueName.KALSHI, "USD")
        mb_before = before.pool(VenueName.MATCHBOOK, "GBP")
        ks_before = before.pool(VenueName.KALSHI, "USD")
        locked_gbp = next(leg.filled_stake for leg in trade.legs if leg.venue is VenueName.MATCHBOOK)
        locked_usd = next(leg.filled_stake for leg in trade.legs if leg.venue is VenueName.KALSHI)
        assert mb.locked_capital == locked_gbp
        assert mb.available_cash == mb_before.available_cash - locked_gbp
        assert ks.locked_capital == locked_usd
        assert ks.available_cash == ks_before.available_cash - locked_usd
        assert after.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
        health = TestClient(app).get("/health").json()
        assert health["execution_enabled"] is False
    finally:
        repository.close()
        ledger.close()


def test_generalized_payoff_autofill_skips_zero_stake_legs(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        matchbook = MatchbookObservationBuilder().build(
            MB_EVENT, _ftts_mb_payload(), observed_at=OBSERVED, quote_age_ms=120
        )
        kalshi = _kalshi_ftts_observation()
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            matchbook,
            kalshi,
            venue_costs=_ftts_kalshi_costs(),
        )
        assert decision.solver_model == SOLVER_MODEL_GENERALIZED
        assert decision.depth_scan is None
        assert decision.payoff_scan is not None
        assert all(stake.stake > 0 for stake in decision.payoff_scan.solution.selected_stakes)
        history = scan.market_intelligence.market_history(
            canonical_market_id=decision.canonical_market_id
        )
        mapped = observation_from_paper_decision(decision, history)
        assert mapped is not None
        assert mapped.implied_probability_sum is None
        assert mapped.current_net_edge is not None
        tracked = watchlist.repository.get(f"watch:{decision.canonical_market_id}")
        assert tracked is not None
        assert tracked.implied_probability_sum is None
        assert tracked.status == OpportunityStatus.FILLED
        trade = ops.list_active_trades()[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.solver_model == SOLVER_MODEL_GENERALIZED
        assert all(leg.filled_stake > 0 for leg in trade.legs)
        assert all(leg.fill_kind is not PaperLegFillKind.UNFILLED for leg in trade.legs)
        _assert_allocator_sized(ops, trade)
        selected = {
            (stake.venue, stake.source_market_id, stake.source_runner_id)
            for stake in decision.payoff_scan.solution.selected_stakes
            if stake.stake > 0
        }
        filled = {(leg.venue, leg.source_market_id, leg.source_runner_id) for leg in trade.legs}
        assert filled <= selected
    finally:
        repository.close()
        ledger.close()


def test_generalized_missing_edge_fails_closed_without_implied_sum(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        matchbook = MatchbookObservationBuilder().build(
            MB_EVENT, _ftts_mb_payload(), observed_at=OBSERVED, quote_age_ms=120
        )
        kalshi = _kalshi_ftts_observation()
        decision = scan.scan_pair(
            matchbook,
            kalshi,
            venue_costs=_ftts_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        assert decision.solver_model == SOLVER_MODEL_GENERALIZED
        assert decision.payoff_scan is not None
        history = scan.market_intelligence.market_history(
            canonical_market_id=decision.canonical_market_id
        )
        mapped = observation_from_paper_decision(decision, history)
        assert mapped is not None
        assert mapped.implied_probability_sum is None
        missing = mapped.model_copy(
            update={
                "current_net_edge": None,
                "solver_is_arbitrage": True,
                "eligible_for_paper_simulation": True,
            }
        )
        opportunity = watchlist.observe(missing)
        assert opportunity.status == OpportunityStatus.REJECTED
        assert "missing_net_edge" in opportunity.rejection_reasons
        assert opportunity.implied_probability_sum is None
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        assert ops.list_active_trades() == []
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert snap.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
    finally:
        repository.close()
        ledger.close()


def test_matchbook_kalshi_autofill_is_internal_on_both_sides(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        assert VenueName.POLYMARKET not in decision.execution_modes
        assert decision.execution_modes[VenueName.KALSHI] == LegExecutionMode.INTERNAL
        assert decision.execution_modes[VenueName.MATCHBOOK] == LegExecutionMode.INTERNAL
        trade = ops.list_active_trades()[0]
        assert trade.state is PaperTradeState.OPEN
        kinds = {leg.fill_kind for leg in trade.legs}
        assert kinds == {PaperLegFillKind.INTERNAL_SIMULATED}
        venues = {leg.venue for leg in trade.legs}
        assert venues == {VenueName.MATCHBOOK, VenueName.KALSHI}
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
        assert snap.pool(VenueName.KALSHI, "USD").locked_capital > 0
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital > 0
    finally:
        repository.close()
        ledger.close()


def test_polymarket_kalshi_pair_is_not_register_admitted(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = scan.scan_pair(
            _polymarket_btts(),
            _kalshi_btts(),
            venue_costs=_pm_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        assert decision.market_match.matched is False
        assert "not_registered" in decision.market_match.reasons
        assert decision.eligible_for_paper_simulation is False
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()


def test_stale_second_leg_fails_closed_and_reconciles_treasury(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=False)
    try:
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        before = ledger.treasury.snapshot()
        thin = plan.legs[-1]
        plan.legs[-1] = thin.model_copy(
            update={
                "quote_age_ms": 50_000,
                "levels": [BookLevel(decimal_odds=thin.displayed_odds, available_stake=thin.requested_stake / 10)],
            }
        )
        ops.settings = _settings(autofill=True)
        with pytest.raises(PaperOperationsError):
            ops.simulate_fill(
                opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.FIXTURE_DEMO,
                now=OBSERVED,
            )
        assert ops.list_active_trades() == []
        after = ledger.treasury.snapshot()
        for venue, currency in (
            (VenueName.MATCHBOOK, "GBP"),
            (VenueName.POLYMARKET, "USD"),
            (VenueName.KALSHI, "USD"),
        ):
            assert after.pool(venue, currency).available_cash == before.pool(venue, currency).available_cash
            assert after.pool(venue, currency).locked_capital == before.pool(venue, currency).locked_capital
        events = watchlist.activity(opportunity_id=opportunity_id)
        assert any(item.event_type is LifecycleEventType.PAPER_FILL_REJECTED for item in events)
        assert opportunity_id in ops._entry_rejections
        opportunity = watchlist.repository.get(opportunity_id)
        assert opportunity is not None
        assert opportunity.status is not OpportunityStatus.FILLED
        assert decision.allocation is not None
    finally:
        repository.close()
        ledger.close()


def test_partial_depth_never_opens_guaranteed_trade(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=False)
    try:
        _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        plan.legs = [
            leg.model_copy(
                update={
                    "levels": [
                        BookLevel(decimal_odds=leg.displayed_odds, available_stake=leg.requested_stake / 8)
                    ]
                }
            )
            if leg.venue is VenueName.MATCHBOOK
            else leg
            for leg in plan.legs
        ]
        result = ops.simulate_fill(
            opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        loaded = ops.list_active_trades()
        assert len(loaded) == 1
        trade = loaded[0]
        assert result.trade_id == trade.trade_id
        assert trade.guaranteed_profit_gbp_at_open is None
        assert trade.state in {PaperTradeState.PARTIAL, PaperTradeState.OPEN}
        if trade.state is PaperTradeState.PARTIAL:
            assert trade.unresolved_recovery is True
            assert (trade.residual_exposure_gbp or Decimal("0")) > 0
        else:
            assert any(item.kind.value == "recovery" for item in trade.tranches) or trade.unresolved_recovery is False
        opportunity = watchlist.repository.get(opportunity_id)
        assert opportunity is not None
        assert opportunity.status is not OpportunityStatus.FILLED or trade.state is PaperTradeState.OPEN
    finally:
        repository.close()
        ledger.close()


def test_repeated_autofill_is_idempotent(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        first_journals = list(ops.journal.list_entries())
        first_snap = ledger.treasury.snapshot()
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        ops.simulate_fill(
            next(iter(ops._plans)),
            simulate_external=True,
            provenance=DataProvenance.LIVE_PAPER,
        )
        assert len(ops.list_active_trades()) == 1
        assert len(ops.journal.list_entries()) == len(first_journals)
        after = ledger.treasury.snapshot()
        for venue in (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI):
            currency = "GBP" if venue is VenueName.MATCHBOOK else "USD"
            assert after.pool(venue, currency).available_cash == first_snap.pool(venue, currency).available_cash
            assert after.pool(venue, currency).locked_capital == first_snap.pool(venue, currency).locked_capital
    finally:
        repository.close()
        ledger.close()


def test_conditionally_releasable_capital_is_not_spendable(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        extra = {
            "conditionally_releasable": {(VenueName.MATCHBOOK, "GBP"): Decimal("300")},
            "liquidity_snapshot": _standing_locked_matchbook(),
        }
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            extra=extra,
        )
        mb_alloc = next(
            row
            for row in decision.allocation.free_balance_after
            if row.venue is VenueName.MATCHBOOK
        )
        assert mb_alloc.conditionally_releasable == Decimal("300")
        assert mb_alloc.allocated_native <= Decimal("700")
        trade = ops.list_active_trades()[0]
        mb_leg = next(leg for leg in trade.legs if leg.venue is VenueName.MATCHBOOK)
        assert mb_leg.filled_stake <= Decimal("700")
        snap = ledger.treasury.snapshot()
        mb = snap.pool(VenueName.MATCHBOOK, "GBP")
        assert mb.locked_capital == mb_leg.filled_stake
        assert mb.available_cash == Decimal("1000") - mb_leg.filled_stake
    finally:
        repository.close()
        ledger.close()


def test_allocator_required_for_autofill(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
        )
        assert decision.eligible_for_paper_simulation is True
        assert decision.allocation is None
        watchlist.observe_paper_decision(
            decision,
            scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
        )
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        assert ops.list_active_trades() == []
        assert any("allocator_size_required" in reason for reason in ops._entry_rejections.values())
    finally:
        repository.close()
        ledger.close()


def test_no_venue_write_trading_paths() -> None:
    for client in (MatchbookClient, PolymarketClient, KalshiClient):
        assert not hasattr(client, "place_order")
        assert not hasattr(client, "cancel_order")
        assert not hasattr(client, "sign")
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"


def test_fixture_detail_and_trades_api_open_only_after_complete(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    client = TestClient(app)
    app.dependency_overrides[get_paper_operations_service] = lambda: ops
    app.dependency_overrides[get_paper_ledger] = lambda: ledger
    try:
        missing = client.get("/paper/trades/active")
        assert missing.status_code == 200
        assert missing.json() == []
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        active = client.get("/paper/trades/active").json()
        assert len(active) == 1
        trade = active[0]
        assert trade["state"] == "OPEN"
        assert trade["solver_model"] == SOLVER_MODEL_SIMPLE
        assert trade["places_orders"] is False
        assert trade["guaranteed_profit_gbp_at_open"] is not None
        kinds = {leg["fill_kind"] for leg in trade["legs"]}
        assert kinds == {"INTERNAL_SIMULATED"}
        assert "MANUAL_EXTERNAL" not in kinds
        treasury = client.get("/paper/treasury").json()
        assert treasury["execution_enabled"] is False
        assert treasury["mode"] == "paper"
        by_venue = {pool["venue"]: pool for pool in treasury["pools"]}
        assert by_venue["polymarket"]["native_currency"] == "USD"
        assert by_venue["kalshi"]["native_currency"] == "USD"
        assert Decimal(by_venue["kalshi"]["locked_capital"]) > 0
        assert Decimal(by_venue["polymarket"]["locked_capital"]) == 0
        coord = get_live_refresh_coordinator()
        coord.record_report(
            CollectionReport(
                started_at=OBSERVED,
                completed_at=OBSERVED,
                discovered_fixtures=[
                    DiscoveredFixture(
                        source_event_id="1001",
                        canonical_event_id=decision.canonical_event_id,
                        home_team="Newcastle United",
                        away_team="Chelsea",
                        competition="Premier League",
                        kickoff_utc=KICKOFF,
                        last_seen_at=OBSERVED,
                    )
                ],
            )
        )
        detail = client.get(f"/operations/fixtures/{decision.canonical_event_id}").json()
        assert detail["execution_enabled"] is False
        assert detail["paper_mode"] == "paper"
        assert len(detail["paper_entries"]) == 1
        assert detail["paper_entries"][0]["state"] == "OPEN"
        assert detail["paper_entries"][0]["places_orders"] is False
    finally:
        app.dependency_overrides.clear()
        repository.close()
        ledger.close()


def test_watchlist_stale_clock_ages_radar_without_open_trade(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=False)
    try:
        _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        opportunity_id = next(iter(ops._plans))
        before = ledger.treasury.snapshot()
        aged = watchlist.triggered(as_of=OBSERVED + timedelta(seconds=30), limit=10)
        assert aged == []
        row = watchlist.repository.get(opportunity_id)
        assert row is not None
        assert row.status is OpportunityStatus.TRIGGERED
        assert "stale_quote" not in row.rejection_reasons
        assert ops.list_active_trades() == []
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").available_cash == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).available_cash
    finally:
        repository.close()
        ledger.close()


def test_qualifying_live_paper_auto_opens_once_with_allocator_size_and_journal(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        before = ledger.treasury.snapshot()
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            provenance=DataProvenance.LIVE_PAPER,
        )
        active = ops.list_active_trades()
        assert len(active) == 1
        trade = active[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.provenance is DataProvenance.LIVE_PAPER
        assert trade.places_orders is False
        assert trade.paper_only is True
        _assert_allocator_sized(ops, trade)
        assert trade.guaranteed_profit_gbp_at_open == decision.allocation.guaranteed_profit
        journals = list(ops.journal.list_entries())
        assert journals
        assert all(entry.provenance is DataProvenance.LIVE_PAPER for entry in journals)
        after = ledger.treasury.snapshot()
        mb = after.pool(VenueName.MATCHBOOK, "GBP")
        ks = after.pool(VenueName.KALSHI, "USD")
        locked_gbp = next(leg.filled_stake for leg in trade.legs if leg.venue is VenueName.MATCHBOOK)
        locked_usd = next(leg.filled_stake for leg in trade.legs if leg.venue is VenueName.KALSHI)
        assert mb.locked_capital == locked_gbp
        assert mb.available_cash == before.pool(VenueName.MATCHBOOK, "GBP").available_cash - locked_gbp
        assert ks.locked_capital == locked_usd
        assert ks.available_cash == before.pool(VenueName.KALSHI, "USD").available_cash - locked_usd
        assert after.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
        health = TestClient(app).get("/health").json()
        assert health["execution_enabled"] is False
        assert health["mode"] == "paper"
    finally:
        repository.close()
        ledger.close()


def test_repeated_hot_cycle_observations_are_idempotent(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        first = ops.list_active_trades()[0]
        first_journals = list(ops.journal.list_entries())
        first_snap = ledger.treasury.snapshot()
        for _ in range(3):
            watchlist.observe_paper_decision(
                decision,
                scan.market_intelligence.market_history(
                    canonical_market_id=decision.canonical_market_id
                ),
            )
            ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        active = ops.list_active_trades()
        assert len(active) == 1
        assert active[0].trade_id == first.trade_id
        assert active[0].state is PaperTradeState.OPEN
        assert len(ops.journal.list_entries()) == len(first_journals)
        after = ledger.treasury.snapshot()
        for venue in (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI):
            currency = "GBP" if venue is VenueName.MATCHBOOK else "USD"
            assert after.pool(venue, currency).available_cash == first_snap.pool(
                venue, currency
            ).available_cash
            assert after.pool(venue, currency).locked_capital == first_snap.pool(
                venue, currency
            ).locked_capital
        assert active[0].guaranteed_profit_gbp_at_open == first.guaranteed_profit_gbp_at_open
    finally:
        repository.close()
        ledger.close()


def test_allocator_rejection_does_not_auto_open_live_paper(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        before = ledger.treasury.snapshot()
        decision = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
        )
        assert decision.eligible_for_paper_simulation is True
        assert decision.allocation is None
        watchlist.observe_paper_decision(
            decision,
            scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
        )
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        assert ops.list_active_trades() == []
        assert any("allocator_size_required" in reason for reason in ops._entry_rejections.values())
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").locked_capital == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).locked_capital
        assert after.pool(VenueName.POLYMARKET, "USD").locked_capital == before.pool(
            VenueName.POLYMARKET, "USD"
        ).locked_capital
    finally:
        repository.close()
        ledger.close()


def test_stale_quote_does_not_veto_bound_min_net_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        kwargs = dict(
            venue_costs=_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        decision = scan.scan_pair(_matchbook_btts(), _kalshi_btts(), **kwargs)
        assert decision.eligible_for_paper_simulation is True
        watchlist.observe_paper_decision(
            decision,
            scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
        )
        stale_legs = [
            leg.model_copy(update={"quote_age_ms": 50_000}) for leg in decision.fill_legs
        ]
        stale = decision.model_copy(update={"quote_age_ms": 50_000, "fill_legs": stale_legs})
        ops.persist_triggered_chain(stale, provenance=DataProvenance.LIVE_PAPER)
        trades = ops.list_active_trades()
        assert len(trades) == 1
        assert trades[0].state is PaperTradeState.OPEN
        assert trades[0].paper_only is True
        assert trades[0].places_orders is False
    finally:
        repository.close()
        ledger.close()


def test_tracked_or_near_row_does_not_auto_open_live_paper(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        kwargs = dict(
            venue_costs=_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        decision = scan.scan_pair(_matchbook_btts(), _kalshi_btts(), **kwargs)
        near = decision.model_copy(update={"eligible_for_paper_simulation": False})
        watchlist.observe_paper_decision(
            near,
            scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
        )
        ops.persist_triggered_chain(near, provenance=DataProvenance.LIVE_PAPER)
        assert ops.list_active_trades() == []
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert snap.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
    finally:
        repository.close()
        ledger.close()


def test_fixture_demo_does_not_inherit_live_paper_autofill(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert decision.eligible_for_paper_simulation is True
        assert ops.list_active_trades() == []
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert snap.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
        fill_journals = [
            entry
            for entry in ops.journal.list_entries()
            if entry.source not in {"paper_treasury_seed"}
        ]
        assert fill_journals == []
    finally:
        repository.close()
        ledger.close()


def test_explicit_autofill_false_overrides_live_paper_setting(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        kwargs = dict(
            venue_costs=_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        decision = scan.scan_pair(_matchbook_btts(), _kalshi_btts(), **kwargs)
        watchlist.observe_paper_decision(
            decision,
            scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
        )
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=False,
        )
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()
