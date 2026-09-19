"""Paper-eligible LIVE_PAPER auto-capture contract.

With AUTO PAPER CAPTURE ON, a scan-eligible Matchbook/Kalshi decision must end
in one OPEN paper trade or one explicit durable capture rejection. Silent
eligible -> nothing is forbidden. Data class: deterministic fixture/demo
paper-scan payloads, not live venue quotes. Phase 1 remains paper-only /
read-only toward venues.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.api import paper as paper_api
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.economics import classify_status
from sports_hedge.arbitrage.watchlist.models import (
    LifecycleEventType,
    OpportunityStatus,
    WatchObservation,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.paper_assumed import PAPER_NONBLOCKING_REJECTION_REASONS
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient
from test_hot_scan_reliability import _hot_leftover_report
from test_step8f_automatic_paper_entry import (
    FX,
    _kalshi_btts,
    _kalshi_costs,
    _matchbook_btts,
    _ops_bundle,
    _standing,
)


MB_K = (VenueName.MATCHBOOK, VenueName.KALSHI)
WRITE_TOKENS = ("place_order", "cancel_order", "sign_order", "submit_order")


def _paper_assumed(decision):
    reasons = list(dict.fromkeys([*decision.rejection_reasons, "paper_assumed_equivalent"]))
    return decision.model_copy(update={"rejection_reasons": reasons})


def _qualify(scan):
    decision = scan.scan_pair(
        _matchbook_btts(),
        _kalshi_btts(),
        venue_costs=_kalshi_costs(),
        fx_snapshots=FX,
        maximum_execution_risk=100,
        liquidity_snapshot=_standing(),
    )
    assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
    assert decision.allocation is not None and decision.allocation.accepted
    return _paper_assumed(decision)


def _observe(scan, watchlist, decision):
    return watchlist.observe_paper_decision(
        decision,
        scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
    )


def _opportunity_id(decision) -> str:
    return f"watch:{decision.canonical_market_id}"


def _capture_rejections(watchlist, opportunity_id: str):
    return [
        event
        for event in watchlist.activity(opportunity_id=opportunity_id)
        if event.event_type is LifecycleEventType.PAPER_FILL_REJECTED
    ]


def test_paper_assumed_classify_is_triggered_not_rejected() -> None:
    observed = WatchObservation(
        observed_at=datetime(2026, 9, 20, 13, tzinfo=UTC),
        canonical_event_id="evt",
        canonical_market_id="mkt",
        market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal("0.0111"),
        implied_probability_sum=Decimal("0.989"),
        solver_is_arbitrage=True,
        eligible_for_paper_simulation=True,
        rejection_reasons=["paper_assumed_equivalent"],
        quote_age_ms=120,
    )
    status, reasons = classify_status(
        observed, approaching_band_pp=Decimal("0.25"), max_quote_age_ms=2000
    )
    assert "paper_assumed_equivalent" in PAPER_NONBLOCKING_REJECTION_REASONS
    assert status is OpportunityStatus.TRIGGERED
    assert "paper_assumed_equivalent" in reasons


def test_paper_assumed_eligible_autofill_opens_once_and_locks(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan)
        watched = _observe(scan, watchlist, decision)
        assert watched is not None
        assert watched.status is OpportunityStatus.TRIGGERED
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            refreshed_venues=MB_K,
        )
        trades = ops.list_active_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.paper_only is True
        assert trade.places_orders is False
        assert trade.guaranteed_profit_gbp_at_open is not None
        assert trade.guaranteed_profit_gbp_at_open > 0
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital > 0
        assert snap.pool(VenueName.KALSHI, "USD").locked_capital > 0
        assert LifecycleEventType.PAPER_FILL_COMPLETE in [
            event.event_type for event in watchlist.activity(opportunity_id=trade.opportunity_id)
        ]

        first_id = trade.trade_id
        first_locks = snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            refreshed_venues=MB_K,
        )
        retried = ops.list_active_trades()
        assert len(retried) == 1
        assert retried[0].trade_id == first_id
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").locked_capital == first_locks
    finally:
        repository.close()
        ledger.close()


def test_eligible_freshness_fail_records_durable_capture_rejection(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan)
        _observe(scan, watchlist, decision)
        stale_legs = [leg.model_copy(update={"quote_age_ms": 50_000}) for leg in decision.fill_legs]
        stale = decision.model_copy(update={"quote_age_ms": 50_000, "fill_legs": stale_legs})
        before = ledger.treasury.snapshot()
        ops.persist_triggered_chain(
            stale, provenance=DataProvenance.LIVE_PAPER, refreshed_venues=MB_K
        )
        assert ops.list_active_trades() == []
        opportunity_id = _opportunity_id(decision)
        assert ops._entry_rejections.get(opportunity_id)
        assert _capture_rejections(watchlist, opportunity_id)
        watched = watchlist.repository.get(opportunity_id)
        assert watched is not None
        assert watched.status is OpportunityStatus.REJECTED
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").locked_capital == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).locked_capital
    finally:
        repository.close()
        ledger.close()


def test_eligible_treasury_fail_records_durable_capture_rejection(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan)
        _observe(scan, watchlist, decision)
        ledger._connection.execute("UPDATE paper_treasury_pools SET available_cash = '0'")
        ledger._connection.commit()
        ops.persist_triggered_chain(
            decision, provenance=DataProvenance.LIVE_PAPER, refreshed_venues=MB_K
        )
        assert ops.list_active_trades() == []
        opportunity_id = _opportunity_id(decision)
        reason = ops._entry_rejections.get(opportunity_id)
        assert reason in {"insufficient_spendable_treasury", "missing_treasury_pool"}
        assert _capture_rejections(watchlist, opportunity_id)
        watched = watchlist.repository.get(opportunity_id)
        assert watched is not None
        assert watched.status is OpportunityStatus.REJECTED
        assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
    finally:
        repository.close()
        ledger.close()


def test_venues_not_refreshed_keeps_gate_and_records_rejection(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan)
        assert any(leg.venue is VenueName.KALSHI for leg in decision.fill_legs)
        watched = _observe(scan, watchlist, decision)
        assert watched is not None
        assert watched.status is OpportunityStatus.TRIGGERED
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            refreshed_venues=(VenueName.MATCHBOOK,),
        )
        assert ops.list_active_trades() == []
        opportunity_id = _opportunity_id(decision)
        assert ops._entry_rejections.get(opportunity_id) == "venues_not_refreshed_this_cycle"
        rejections = _capture_rejections(watchlist, opportunity_id)
        assert rejections
        assert "venues_not_refreshed_this_cycle" in (rejections[0].detail or "")
        watched = watchlist.repository.get(opportunity_id)
        assert watched is not None
        assert watched.status is OpportunityStatus.REJECTED

        watched = _observe(scan, watchlist, decision)
        assert watched is not None
        assert watched.status is OpportunityStatus.TRIGGERED
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            refreshed_venues=MB_K,
        )
        opened = ops.list_active_trades()
        assert len(opened) == 1
        assert opened[0].state is PaperTradeState.OPEN
    finally:
        repository.close()
        ledger.close()


def test_autofill_off_does_not_capture_or_invent_rejection(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=False)
    try:
        decision = _qualify(scan)
        watched = _observe(scan, watchlist, decision)
        assert watched is not None
        assert watched.status is OpportunityStatus.TRIGGERED
        ops.persist_triggered_chain(
            decision, provenance=DataProvenance.LIVE_PAPER, refreshed_venues=MB_K
        )
        assert ops.list_active_trades() == []
        opportunity_id = _opportunity_id(decision)
        assert opportunity_id not in ops._entry_rejections
        assert _capture_rejections(watchlist, opportunity_id) == []
        watched = watchlist.repository.get(opportunity_id)
        assert watched is not None
        assert watched.status is OpportunityStatus.TRIGGERED
        assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
    finally:
        repository.close()
        ledger.close()


def test_empty_opening_legs_record_durable_rejection(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan)
        _observe(scan, watchlist, decision)
        empty_legs = [
            leg.model_copy(update={"requested_stake": Decimal("0")}) for leg in decision.fill_legs
        ]
        empty = decision.model_copy(update={"fill_legs": empty_legs})
        ops.persist_triggered_chain(
            empty, provenance=DataProvenance.LIVE_PAPER, refreshed_venues=MB_K
        )
        assert ops.list_active_trades() == []
        opportunity_id = _opportunity_id(decision)
        assert ops._entry_rejections.get(opportunity_id) == "no_positive_opening_legs"
        assert _capture_rejections(watchlist, opportunity_id)
    finally:
        repository.close()
        ledger.close()


def test_operations_factory_refreshes_cached_autofill_flag(monkeypatch) -> None:
    watchlist = WatchlistService(SqliteWatchlistRepository())
    alerts = PriorityAlertService()
    stale = Settings(paper_autofill_enabled=False)
    live = Settings(paper_autofill_enabled=True)
    holder = PaperOperationsService(watchlist=watchlist, alerts=alerts, settings=stale)
    assert holder._should_autofill(autofill=None, provenance=DataProvenance.LIVE_PAPER) is False
    monkeypatch.setattr(paper_api, "get_paper_journal_holder", lambda: holder)
    monkeypatch.setattr(paper_api, "get_settings", lambda: live)
    resolved = paper_api.get_paper_operations_service(watchlist, alerts)
    assert resolved is holder
    assert resolved.settings.paper_autofill_enabled is True
    assert resolved._should_autofill(autofill=None, provenance=DataProvenance.LIVE_PAPER) is True


@pytest.mark.asyncio
async def test_hot_and_universe_persist_share_capture_contract(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        decision = _qualify(scan)

        def operations_factory(watchlist_arg=None, alerts=None):
            del alerts
            if watchlist_arg is not None:
                ops.watchlist = watchlist_arg
            return ops

        monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
        coordinator = LiveRefreshCoordinator()
        coordinator.reset()
        for lane in (ScanLane.HOT, ScanLane.UNIVERSE):
            report = _hot_leftover_report(cancelled=False).model_copy(
                update={
                    "paper_decisions": [decision],
                    "enabled_venues": list(MB_K),
                    "scan_lane": lane.value,
                }
            )
            await paper_api.persist_scheduled_collection_report(
                coordinator,
                report,
                service=scan,
                audit=audit,
                watchlist=watchlist,
                scan_lane=lane,
            )
            opened = ops.list_active_trades()
            assert len(opened) == 1, lane
            assert opened[0].state is PaperTradeState.OPEN
            assert opened[0].paper_only is True
            assert opened[0].places_orders is False
    finally:
        repository.close()
        ledger.close()
        audit.close()


def test_paper_only_boundary_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    source = inspect.getsource(PaperOperationsService.persist_triggered_chain)
    assert "PAPER-ONLY autofill; no venue order placed" in source
    for client in (MatchbookClient, PolymarketClient, KalshiClient):
        assert client.capabilities.execution_enabled is False
        for token in WRITE_TOKENS:
            assert not hasattr(client, token)
