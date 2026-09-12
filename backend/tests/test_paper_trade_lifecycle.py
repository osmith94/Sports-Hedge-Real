from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.accounting.paper_journal import DataProvenance, gbp_is_balanced
from sports_hedge.api.main import app
from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.api.paper import get_paper_audit_repository, get_paper_operations_service
from sports_hedge.api.priority_alerts import get_priority_alert_service
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.models import ExternalLegConfirmation
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.fees.effective import profit_commission_net_odds
from sports_hedge.paper.fills import PaperFillConfig
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.settlement import compute_paper_settlement
from sports_hedge.paper.trades import (
    PaperLegFillKind,
    PaperSettlementRequest,
    PaperTradeLeg,
    PaperTradeState,
)
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.treasury.models import TreasuryLockRequest
from test_paper_scan_pipeline import matchbook_payloads, polymarket_payloads
from venue_cost_helpers import matchbook_polymarket_costs


OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def independent_realised_pnl_gbp(trade, winning_outcome: str) -> Decimal:
    """Textbook profit-commission P&L from recorded fills, fees and FX.

    Independent of `compute_paper_settlement` so double-counted fees fail this proof.
    Unfilled legs contribute nothing.

    Winning native P&L = stake * (1 + (odds-1)*(1-rate)) - stake
                       = stake * (odds-1) * (1-rate)
    Losing native P&L  = -stake
    GBP                = native * stored gbp_per_unit (1 for GBP)
    """

    fx = {item.currency: item.gbp_per_unit for item in trade.fx_snapshots}
    fx.setdefault("GBP", Decimal("1"))
    total = Decimal("0")
    for leg in trade.legs:
        if leg.filled_stake <= 0:
            continue
        gbp_per = fx[leg.currency]
        if leg.outcome != winning_outcome:
            total += -leg.filled_stake * gbp_per
            continue
        cost = _cost_for_leg(trade.venue_costs, leg)
        if cost.rate is None:
            raise AssertionError(f"missing fee rate for {leg.venue}")
        odds = leg.filled_odds or leg.displayed_odds
        if odds is None:
            raise AssertionError(f"missing filled odds for {leg.outcome}")
        net_odds = profit_commission_net_odds(odds, cost.rate)
        total += (leg.filled_stake * net_odds - leg.filled_stake) * gbp_per
    return total


def _cost_for_leg(costs, leg):
    matches = [item for item in costs if item.venue is leg.venue]
    if len(matches) == 1:
        return matches[0]
    by_market = [item for item in matches if item.source_market_id == leg.source_market_id]
    if len(by_market) == 1:
        return by_market[0]
    raise AssertionError(f"ambiguous/missing venue cost for {leg.venue}")


def assert_settlement_arithmetic(trade, winning_outcome: str, expected: Decimal) -> None:
    computation = compute_paper_settlement(trade, winning_outcome=winning_outcome)
    assert computation.realised_pnl_gbp == expected
    gbp_sum = Decimal("0")
    for item in computation.legs:
        assert item.filled_stake > 0
        if item.won:
            assert item.filled_odds is not None
            assert item.gross_payoff == item.filled_stake * item.filled_odds
            assert item.venue_fee == item.gross_payoff - item.net_payoff
            assert item.native_pnl == item.net_payoff - item.filled_stake
        else:
            assert item.venue_fee == Decimal("0")
            assert item.net_payoff == Decimal("0")
            assert item.gross_payoff == Decimal("0")
            assert item.native_pnl == -item.filled_stake
        assert item.gbp_pnl == item.native_pnl * item.fx_rate_gbp_per_unit
        gbp_sum += item.gbp_pnl
    assert computation.realised_pnl_gbp == gbp_sum


def _ops(
    *,
    ledger: SqlitePaperLedger,
    autofill: bool = False,
) -> tuple[PaperScanService, WatchlistService, PaperOperationsService, SqliteMarketIntelligenceRepository]:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    settings = Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=autofill,
    )
    scan = PaperScanService(intelligence, settings=settings)
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    alerts = PriorityAlertService(
        thresholds=PriorityAlertThresholds(
            minimum_net_edge=Decimal("0.001"),
            minimum_expected_profit=Decimal("0.01"),
            minimum_executable_depth=Decimal("1"),
            maximum_quote_age_ms=10_000,
            maximum_execution_risk=100,
            minimum_depth_coverage=Decimal("0"),
            minimum_capital_efficiency=Decimal("0"),
        ),
        settings=settings,
    )
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=alerts,
        settings=settings,
        ledger=ledger,
    )
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    decision = scan.scan_pair(
        matchbook,
        polymarket,
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), spread_bps=Decimal("0"))],
        maximum_execution_risk=100,
    )
    assert decision.eligible_for_paper_simulation is True
    watchlist.observe_paper_decision(
        decision,
        intelligence.market_history(canonical_market_id=decision.canonical_market_id),
    )
    ops.persist_triggered_chain(decision, provenance=DataProvenance.FIXTURE_DEMO)
    return scan, watchlist, ops, repository


def test_autofill_creates_exactly_one_open_trade(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        active = ops.list_active_trades()
        assert len(active) == 1
        trade = active[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.paper_only is True
        assert trade.places_orders is False
        kinds = {leg.fill_kind for leg in trade.legs}
        assert PaperLegFillKind.PAPER_SIMULATED_EXTERNAL in kinds
        assert PaperLegFillKind.MANUAL_EXTERNAL not in kinds
        sources = {entry.source for entry in ops.journal.list_entries()}
        assert "paper_simulated_external" in sources
        assert "manual_external_confirmation" not in sources
        assert "GBP" in trade.capital_locked_native
        assert "USD" in trade.capital_locked_native
        assert trade.capital_locked_native["GBP"] != trade.capital_locked_native["USD"]
        assert trade.guaranteed_profit_gbp_at_open is not None
        assert trade.realised_pnl_gbp is None
        ops.persist_triggered_chain(ops._plans[trade.opportunity_id].decision, provenance=DataProvenance.FIXTURE_DEMO)
        assert len(ops.list_active_trades()) == 1
        assert len(ops.journal.list_entries()) == len({entry.source_id for entry in ops.journal.list_entries()})
    finally:
        repository.close()
        ledger.close()


def test_repeated_refresh_does_not_duplicate_journal(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        first = list(ops.journal.list_entries())
        trade = ops.list_active_trades()[0]
        ops.simulate_fill(trade.opportunity_id, simulate_external=True, provenance=DataProvenance.FIXTURE_DEMO)
        ops.simulate_fill(trade.opportunity_id, simulate_external=True, provenance=DataProvenance.FIXTURE_DEMO)
        assert len(ops.journal.list_entries()) == len(first)
        assert len(ops.list_active_trades()) == 1
    finally:
        repository.close()
        ledger.close()


def test_manual_external_is_distinct_from_paper_simulated(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        with pytest.raises(PaperOperationsError, match="manual_external_confirmation_required"):
            ops.simulate_fill(opportunity_id, config=PaperFillConfig(assumed_latency_ms=0, max_quote_age_ms=10_000))
        awaiting = ops.list_active_trades()
        assert len(awaiting) == 1
        assert awaiting[0].state is PaperTradeState.AWAITING_MANUAL_EXTERNAL
        assert len(awaiting[0].legs) == len(plan.legs)
        assert all(leg.fill_kind is PaperLegFillKind.UNFILLED for leg in awaiting[0].legs)
        assert all(leg.filled_stake == 0 for leg in awaiting[0].legs)
        assert {(leg.venue, leg.outcome) for leg in awaiting[0].legs} == {
            (leg.venue, leg.outcome) for leg in plan.legs
        }
        pending_external = next(leg for leg in awaiting[0].legs if leg.venue is VenueName.POLYMARKET)
        assert pending_external.execution_mode == "EXTERNAL_OPERATOR"
        assert pending_external.capital_source is CapitalSource.MANUAL_EXTERNAL
        assert pending_external.requested_stake > 0
        assert pending_external.source_market_id
        external = next(leg for leg in plan.legs if leg.venue is VenueName.POLYMARKET)
        from sports_hedge.application.paper_operations import _net_odds_for_leg

        ok = ExternalLegConfirmation(
            outcome=external.outcome,
            venue=external.venue,
            product_id=external.source_market_id,
            operator_counterparty_reference="ext-ok",
            executed_price=_net_odds_for_leg(plan, external),
            executed_size=Decimal("5"),
            currency=external.currency,
            executed_at=OBSERVED,
            eligibility_confirmed=True,
        )
        result = ops.simulate_fill(
            opportunity_id,
            config=PaperFillConfig(assumed_latency_ms=0, max_quote_age_ms=10_000),
            confirm_external=ok,
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert result.hedge_still_valid is True
        trade = ops.list_active_trades()[0]
        kinds = {leg.fill_kind for leg in trade.legs}
        assert PaperLegFillKind.MANUAL_EXTERNAL in kinds
        assert PaperLegFillKind.PAPER_SIMULATED_EXTERNAL not in kinds
        sources = {entry.source for entry in result.journals}
        assert "manual_external_confirmation" in sources
        assert "paper_simulated_external" not in sources
        capital = {leg.capital_source for leg in trade.legs}
        assert CapitalSource.MANUAL_EXTERNAL in capital
        assert CapitalSource.PAPER_SIMULATED_EXTERNAL not in capital
    finally:
        repository.close()
        ledger.close()


def test_partial_settlement_when_unfilled_canonical_outcome_wins(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "partial.sqlite")
    _scan, watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        opportunity = watchlist.repository.get(opportunity_id)
        assert opportunity is not None
        keep_plan, drop_plan = plan.legs[0], plan.legs[1]
        fx = {item.currency: item.gbp_per_unit for item in plan.fx_snapshots}
        fx.setdefault("GBP", Decimal("1"))
        keep_rate = fx[keep_plan.currency]
        trade = ops._new_trade_shell(plan, opportunity, OBSERVED, DataProvenance.FIXTURE_DEMO)
        trade.state = PaperTradeState.PARTIAL
        trade.legs = [
            PaperTradeLeg(
                venue=keep_plan.venue,
                outcome=keep_plan.outcome,
                currency=keep_plan.currency,
                requested_stake=keep_plan.requested_stake,
                filled_stake=keep_plan.requested_stake,
                displayed_odds=keep_plan.displayed_odds,
                filled_odds=keep_plan.displayed_odds,
                source_market_id=keep_plan.source_market_id,
                fill_id="paper-fill-partial-keep",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
                capital_source=CapitalSource.AUTO_POOL,
                execution_mode="INTERNAL",
            ),
            PaperTradeLeg(
                venue=drop_plan.venue,
                outcome=drop_plan.outcome,
                currency=drop_plan.currency,
                requested_stake=drop_plan.requested_stake,
                filled_stake=Decimal("0"),
                displayed_odds=drop_plan.displayed_odds,
                filled_odds=None,
                source_market_id=drop_plan.source_market_id,
                fill_id=None,
                fill_kind=PaperLegFillKind.UNFILLED,
                capital_source=CapitalSource.AUTO_POOL,
                execution_mode="INTERNAL",
            ),
        ]
        trade.capital_locked_native = {keep_plan.currency: keep_plan.requested_stake}
        trade.capital_locked_gbp = keep_plan.requested_stake * keep_rate
        trade.fx_snapshots = list(plan.fx_snapshots)
        trade.venue_costs = list(plan.venue_costs)
        ops.trades.save(trade)
        treasury_rate = ledger.treasury.lock_fx_rate(keep_plan.venue, keep_plan.currency)
        ledger.treasury.lock_capital(
            [
                TreasuryLockRequest(
                    venue=keep_plan.venue,
                    native_currency=keep_plan.currency,
                    amount_native=keep_plan.requested_stake,
                    lock_id="paper-fill-partial-keep",
                    trade_id=trade.trade_id,
                    opportunity_id=plan.opportunity_id,
                    fx_rate_gbp_per_unit=treasury_rate,
                )
            ],
            occurred_at=OBSERVED,
        )

        with pytest.raises(PaperOperationsError, match="settlement_outcome_not_on_trade"):
            ops.settle(
                trade.trade_id,
                PaperSettlementRequest(
                    winning_outcome="not-on-this-market",
                    source="fixture_test",
                    source_id="bogus",
                    provenance=DataProvenance.FIXTURE_DEMO,
                ),
            )

        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        computation = compute_paper_settlement(persisted, winning_outcome=drop_plan.outcome)
        assert all(item.filled_stake > 0 for item in computation.legs)
        assert drop_plan.outcome not in {item.outcome for item in computation.legs}
        assert all(item.won is False for item in computation.legs)
        expected = independent_realised_pnl_gbp(persisted, drop_plan.outcome)
        assert expected == -keep_plan.requested_stake * keep_rate
        assert computation.realised_pnl_gbp == expected
        assert_settlement_arithmetic(persisted, drop_plan.outcome, expected)

        settled = ops.settle(
            trade.trade_id,
            PaperSettlementRequest(
                winning_outcome=drop_plan.outcome,
                source="fixture_test",
                source_id="partial-unfilled-wins",
                settled_at=OBSERVED,
                provenance=DataProvenance.FIXTURE_DEMO,
            ),
        )
        assert settled.state is PaperTradeState.CLOSED
        assert settled.settlement_outcome == drop_plan.outcome
        assert settled.realised_pnl_gbp == expected
        postings = ops.journal.postings(opportunity_id=opportunity_id)
        assert gbp_is_balanced(postings)
        settle_entries = [
            entry
            for entry in ops.journal.list_entries(opportunity_id=opportunity_id)
            if entry.source == "paper_settlement"
        ]
        assert len(settle_entries) == 1
        assert any(posting.account_code.startswith("PNL:BETTING") for posting in settle_entries[0].postings)
    finally:
        repository.close()
        ledger.close()


@pytest.mark.parametrize("outcome_index", [0, 1])
def test_settlement_moves_trade_to_closed_history(tmp_path: Path, outcome_index: int) -> None:
    ledger = SqlitePaperLedger(tmp_path / f"paper-{outcome_index}.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        detail = ops.trade_detail(trade.trade_id)
        assert detail.legs
        assert detail.audit
        assert detail.journals
        outcomes = sorted({leg.outcome for leg in trade.legs})
        winning = outcomes[outcome_index]
        expected_pnl = independent_realised_pnl_gbp(trade, winning)
        assert_settlement_arithmetic(trade, winning, expected_pnl)
        settled = ops.settle(
            trade.trade_id,
            PaperSettlementRequest(
                winning_outcome=winning,
                source="fixture_test",
                source_id=f"result-{winning.lower()}",
                settled_at=OBSERVED,
                provenance=DataProvenance.FIXTURE_DEMO,
            ),
        )
        assert settled.state is PaperTradeState.CLOSED
        assert settled.settlement_outcome == winning
        assert settled.settlement_source == "fixture_test"
        assert settled.realised_pnl_gbp == expected_pnl
        assert settled.trade_id not in {item.trade_id for item in ops.list_active_trades()}
        assert settled.trade_id in {item.trade_id for item in ops.list_closed_trades()}
        postings = ops.journal.postings(opportunity_id=trade.opportunity_id)
        assert gbp_is_balanced(postings)
        again = ops.settle(
            trade.trade_id,
            PaperSettlementRequest(
                winning_outcome=winning,
                source="fixture_test",
                source_id=f"result-{winning.lower()}",
                provenance=DataProvenance.FIXTURE_DEMO,
            ),
        )
        settle_entries = [
            entry for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id) if entry.source == "paper_settlement"
        ]
        assert len(settle_entries) == 1
        assert again.state is PaperTradeState.CLOSED
        with pytest.raises(PaperOperationsError, match="conflicting_settlement"):
            ops.settle(
                trade.trade_id,
                PaperSettlementRequest(
                    winning_outcome="__conflict__",
                    source="fixture_test",
                    source_id="other-result",
                    provenance=DataProvenance.FIXTURE_DEMO,
                ),
            )
    finally:
        repository.close()
        ledger.close()


def test_journal_survives_reinstantiation(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite"
    ledger = SqlitePaperLedger(path)
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        before_trades = ops.list_active_trades()
        before_journals = ops.journal.list_entries()
        assert before_trades and before_journals
        ledger.close()
        reopened = SqlitePaperLedger(path)
        assert len(reopened.trades.list_active()) == 1
        assert len(reopened.journal.list_entries()) == len(before_journals)
        assert reopened.journal.list_entries()[0].source_id == before_journals[0].source_id
        reopened.close()
    finally:
        repository.close()


def test_trade_api_and_paper_page_are_not_mock_portfolio(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "api.sqlite")
    _scan, watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    audit = SqlitePaperScanRepository()
    app.dependency_overrides[get_market_intelligence_service] = lambda: _scan.market_intelligence
    app.dependency_overrides[get_paper_audit_repository] = lambda: audit
    app.dependency_overrides[get_watchlist_service] = lambda: watchlist
    app.dependency_overrides[get_paper_operations_service] = lambda: ops
    app.dependency_overrides[get_priority_alert_service] = lambda: ops.alerts
    client = TestClient(app)
    try:
        active = client.get("/paper/trades/active")
        assert active.status_code == 200
        rows = active.json()
        assert len(rows) == 1
        assert ":" not in rows[0]["trade_id"]
        assert rows[0]["state"] == "OPEN"
        assert "15000" not in active.text
        assert "15.82" not in active.text
        detail = client.get(f"/paper/trades/{rows[0]['trade_id']}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["audit"]
        assert body["journals"]
        assert body["guaranteed_profit_gbp_at_open"] is not None
        summary = client.get("/paper/trades/summary").json()
        assert summary["open_count"] == 1
        assert summary["data_kind"] == "persisted_paper_trades"
        open_trade = ops.trade_detail(rows[0]["trade_id"])
        outcome = rows[0]["legs"][0]["outcome"]
        expected_open_pnl = independent_realised_pnl_gbp(open_trade, outcome)
        settled = client.post(
            f"/paper/trades/{rows[0]['trade_id']}/settle",
            json={
                "winning_outcome": outcome,
                "source": "fixture_test",
                "source_id": "api-1",
                "provenance": "fixture_demo",
            },
        )
        assert settled.status_code == 200
        assert settled.json()["state"] == "CLOSED"
        assert Decimal(str(settled.json()["realised_pnl_gbp"])) == expected_open_pnl
        assert client.get("/paper/trades/active").json() == []
        closed = client.get("/paper/trades/closed").json()
        assert len(closed) == 1
        assert Decimal(str(closed[0]["realised_pnl_gbp"])) == expected_open_pnl
    finally:
        app.dependency_overrides.clear()
        audit.close()
        repository.close()
        ledger.close()


def test_awaiting_external_api_returns_unfilled_planned_legs(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "awaiting-api.sqlite")
    _scan, watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    audit = SqlitePaperScanRepository()
    app.dependency_overrides[get_market_intelligence_service] = lambda: _scan.market_intelligence
    app.dependency_overrides[get_paper_audit_repository] = lambda: audit
    app.dependency_overrides[get_watchlist_service] = lambda: watchlist
    app.dependency_overrides[get_paper_operations_service] = lambda: ops
    app.dependency_overrides[get_priority_alert_service] = lambda: ops.alerts
    client = TestClient(app)
    try:
        opportunity_id = next(iter(ops._plans))
        blocked = client.post("/paper/simulate-fill", json={"opportunity_id": opportunity_id})
        assert blocked.status_code == 409
        active = client.get("/paper/trades/active").json()
        assert len(active) == 1
        assert active[0]["state"] == "AWAITING_MANUAL_EXTERNAL"
        assert active[0]["legs"]
        assert all(leg["fill_kind"] == "UNFILLED" for leg in active[0]["legs"])
        plan = ops._plans[opportunity_id]
        detail = client.get(f"/paper/trades/{active[0]['trade_id']}").json()
        assert {leg["outcome"] for leg in detail["legs"]} == {leg.outcome for leg in plan.legs}
        assert any(leg["execution_mode"] == "EXTERNAL_OPERATOR" for leg in detail["legs"])
        for planned in plan.legs:
            row = next(
                item
                for item in detail["legs"]
                if item["venue"] == planned.venue.value and item["outcome"] == planned.outcome
            )
            assert Decimal(str(row["requested_stake"])) == planned.requested_stake
            assert Decimal(str(row["filled_stake"])) == Decimal("0")
            assert row["currency"] == planned.currency
            assert row["source_market_id"] == planned.source_market_id
            assert row["filled_odds"] is None
    finally:
        app.dependency_overrides.clear()
        audit.close()
        repository.close()
        ledger.close()


def test_step5_introduces_no_execution_capability() -> None:
    from sports_hedge.application import paper_operations
    from sports_hedge.paper import settlement, trades
    from sports_hedge.persistence import paper_ledger

    for module in (paper_operations, settlement, trades, paper_ledger):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("place_order", "cancel_order", "wallet signing", "private_key"):
            assert forbidden not in source
    page = Path(__file__).resolve().parents[2] / "frontend/app/paper/page.tsx"
    text = page.read_text(encoding="utf-8")
    assert "£15,000" not in text
    assert "£15.82" not in text
    assert "paperPositions" not in text
    assert "PaperTradeBook" in text
