"""Issue #264: snapshot-bound paper-entry freshness.

Paper fill ages the qualified snapshot (T1) by configured simulated latency
only. Backend dispatch delay (T2 − T1) is telemetry. Data class: deterministic
fixture/demo paper-scan payloads, not live venue quotes. Phase 1 remains
paper-only / read-only toward venues.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.executable_liquidity import DEFAULT_OPENING_MAX_QUOTE_AGE_MS
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.entry_freshness import (
    SNAPSHOT_STALE_AT_DECISION,
    SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL,
    assess_paper_entry_freshness,
    snapshot_freshness_rejection,
)
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient
from test_step8f_automatic_paper_entry import (
    FX,
    _assert_allocator_sized,
    _matchbook_btts,
    _polymarket_btts,
    _standing,
)
from venue_cost_helpers import matchbook_polymarket_costs

T1 = datetime(2026, 9, 17, 17, 0, 0, 300000, tzinfo=UTC)


def _freshness_bundle(
    tmp_path: Path,
    *,
    autofill: bool,
    latency_ms: int = 500,
    max_age_ms: int = 2000,
    watchlist_max_age_ms: int | None = None,
):
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    repository = SqliteMarketIntelligenceRepository()
    settings = Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=latency_ms,
        paper_entry_max_quote_age_ms=max_age_ms,
        paper_autofill_enabled=autofill,
    )
    scan = PaperScanService(MarketIntelligenceService(repository), settings=settings)
    watchlist = WatchlistService(
        SqliteWatchlistRepository(),
        max_quote_age_ms=watchlist_max_age_ms if watchlist_max_age_ms is not None else max_age_ms,
    )
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    return scan, watchlist, ops, repository, ledger, settings


def _with_quote_age(decision, age_ms: int, *, scanned_at: datetime = T1):
    captured = scanned_at - timedelta(milliseconds=age_ms)
    legs = [
        leg.model_copy(update={"quote_age_ms": age_ms, "quote_captured_at": captured})
        for leg in decision.fill_legs
    ]
    return decision.model_copy(
        update={"quote_age_ms": age_ms, "fill_legs": legs, "scanned_at": scanned_at}
    )


def _qualify(scan, *, age_ms: int, scanned_at: datetime = T1):
    decision = scan.scan_pair(
        _matchbook_btts(),
        _polymarket_btts(),
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=FX,
        maximum_execution_risk=100,
        liquidity_snapshot=_standing(),
    )
    assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
    assert decision.allocation is not None and decision.allocation.accepted
    return _with_quote_age(decision, age_ms, scanned_at=scanned_at)


def _observe(scan, watchlist, decision):
    return watchlist.observe_paper_decision(
        decision,
        scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
    )


def _lock_rows(ledger, trade_id: str) -> list[dict]:
    rows = ledger._connection.execute(
        "SELECT * FROM paper_treasury_locks WHERE trade_id = ? ORDER BY lock_id",
        (trade_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def test_settings_paper_entry_freshness_defaults() -> None:
    settings = Settings()
    assert settings.paper_entry_max_quote_age_ms == 2000
    assert settings.simulated_latency_ms == 500
    assert settings.paper_entry_max_quote_age_ms == DEFAULT_OPENING_MAX_QUOTE_AGE_MS
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"


def test_snapshot_freshness_ignores_backend_dispatch_delay() -> None:
    freshness = assess_paper_entry_freshness(
        decision_at=T1,
        dispatched_at=T1 + timedelta(milliseconds=1500),
        quote_age_at_decision_ms=300,
        simulated_latency_ms=500,
        paper_entry_max_quote_age_ms=2000,
    )
    assert freshness.accepted is True
    assert freshness.simulated_arrival_quote_age_ms == 800
    assert freshness.decision_to_autofill_dispatch_ms == 1500
    assert freshness.quote_age_at_decision_ms == 300
    assert snapshot_freshness_rejection(
        quote_age_at_decision_ms=300,
        simulated_latency_ms=500,
        paper_entry_max_quote_age_ms=2000,
    ) is None


def test_backend_delay_does_not_create_false_stale_rejection(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, settings = _freshness_bundle(tmp_path, autofill=False)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        assert seeded.status is OpportunityStatus.TRIGGERED
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
        opportunity_id = seeded.opportunity_id
        dispatch_at = T1 + timedelta(milliseconds=1500)
        presented = watchlist.triggered(as_of=dispatch_at, limit=10)
        assert [row.opportunity_id for row in presented] == [opportunity_id]
        result = ops.simulate_fill(
            opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.LIVE_PAPER,
            now=dispatch_at,
        )
        freshness = result.entry_freshness
        assert freshness is not None
        assert freshness.simulated_arrival_quote_age_ms == 800
        assert freshness.decision_to_autofill_dispatch_ms == 1500
        assert freshness.quote_age_at_decision_ms == 300
        assert freshness.simulated_latency_ms == 500
        assert freshness.paper_entry_max_quote_age_ms == 2000
        assert freshness.accepted is True
        trades = ops.list_active_trades()
        assert len(trades) == 1
        assert trades[0].state is PaperTradeState.OPEN
        assert trades[0].places_orders is False
        assert trades[0].paper_only is True
        assert settings.sports_hedge_execution_enabled is False
    finally:
        repository.close()
        ledger.close()


def test_presentation_aging_after_trigger_does_not_false_stale_bound_entry(
    tmp_path: Path,
) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=False)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
        dispatch_at = T1 + timedelta(milliseconds=1800)
        aged = watchlist.triggered(as_of=dispatch_at, limit=10)
        assert aged == []
        row = watchlist.repository.get(seeded.opportunity_id)
        assert row is not None
        assert row.status is OpportunityStatus.REJECTED
        assert "stale_quote" in row.rejection_reasons
        result = ops.simulate_fill(
            seeded.opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.LIVE_PAPER,
            now=dispatch_at,
        )
        assert result.entry_freshness is not None
        assert result.entry_freshness.simulated_arrival_quote_age_ms == 800
        assert result.entry_freshness.decision_to_autofill_dispatch_ms == 1800
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        attempted = [
            event
            for event in watchlist.activity(opportunity_id=seeded.opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_FILL_ATTEMPTED
        ]
        complete = [
            event
            for event in watchlist.activity(opportunity_id=seeded.opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_FILL_COMPLETE
        ]
        assert attempted
        assert complete
    finally:
        repository.close()
        ledger.close()


def test_genuine_stale_at_simulated_arrival_fails(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=False)
    try:
        decision = _qualify(scan, age_ms=1700)
        seeded = _observe(scan, watchlist, decision)
        assert seeded.status is OpportunityStatus.TRIGGERED
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
        with pytest.raises(PaperOperationsError, match=SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL):
            ops.simulate_fill(
                seeded.opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
                now=T1 + timedelta(milliseconds=1500),
            )
        plan = ops._plans[seeded.opportunity_id]
        assert plan.entry_freshness is not None
        assert plan.entry_freshness.quote_age_at_decision_ms == 1700
        assert plan.entry_freshness.simulated_arrival_quote_age_ms == 2200
        assert plan.entry_freshness.decision_to_autofill_dispatch_ms == 1500
        assert ops._entry_rejections[seeded.opportunity_id] == SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL
        assert ops.list_active_trades() == []
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert snap.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
    finally:
        repository.close()
        ledger.close()


def test_stale_at_decision_fails(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=False)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
        stale = _with_quote_age(decision, 2000)
        ops._plans[seeded.opportunity_id] = ops._plan_from_decision(
            stale, seeded.opportunity_id, DataProvenance.LIVE_PAPER
        )
        with pytest.raises(PaperOperationsError, match=SNAPSHOT_STALE_AT_DECISION):
            ops.simulate_fill(
                seeded.opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
                now=T1 + timedelta(milliseconds=100),
            )
        assert ops.list_active_trades() == []
        assert ops._entry_rejections[seeded.opportunity_id] == SNAPSHOT_STALE_AT_DECISION
    finally:
        repository.close()
        ledger.close()


def test_later_market_reversal_does_not_cancel_accepted_paper_fill(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
        )
        trade = ops.list_active_trades()[0]
        assert trade.state is PaperTradeState.OPEN
        later = decision.model_copy(
            update={
                "scanned_at": T1 + timedelta(seconds=2),
                "eligible_for_paper_simulation": False,
                "rejection_reasons": ["net_edge_below_threshold"],
            }
        )
        _observe(scan, watchlist, later)
        ops.persist_triggered_chain(later, provenance=DataProvenance.LIVE_PAPER, autofill=True)
        active = ops.list_active_trades()
        assert len(active) == 1
        assert active[0].trade_id == trade.trade_id
        assert active[0].state is PaperTradeState.OPEN
        filled = watchlist.repository.get(seeded.opportunity_id)
        assert filled is not None
        assert filled.status is OpportunityStatus.FILLED
    finally:
        repository.close()
        ledger.close()


def test_auto_entry_is_exactly_once_idempotent(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=True)
        first = ops.list_active_trades()[0]
        fill_ids = [leg.fill_id for leg in first.legs if leg.fill_id]
        assert len(fill_ids) == len(set(fill_ids)) == 2
        first_locks = _lock_rows(ledger, first.trade_id)
        assert len(first_locks) == 2
        first_journals = [
            entry for entry in ops.journal.list_entries() if entry.source != "paper_treasury_seed"
        ]
        events = watchlist.activity(opportunity_id=seeded.opportunity_id)
        assert (
            sum(event.event_type is LifecycleEventType.PAPER_FILL_COMPLETE for event in events) == 1
        )
        for _ in range(3):
            _observe(scan, watchlist, decision)
            ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=True)
            ops.simulate_fill(
                seeded.opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
                now=T1 + timedelta(milliseconds=200),
            )
        active = ops.list_active_trades()
        assert len(active) == 1
        assert active[0].trade_id == first.trade_id
        assert [leg.fill_id for leg in active[0].legs if leg.fill_id] == fill_ids
        assert _lock_rows(ledger, first.trade_id) == first_locks
        journals = [
            entry for entry in ops.journal.list_entries() if entry.source != "paper_treasury_seed"
        ]
        assert len(journals) == len(first_journals)
        complete = [
            event
            for event in watchlist.activity(opportunity_id=seeded.opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_FILL_COMPLETE
        ]
        assert len(complete) == 1
        _assert_allocator_sized(ops, active[0])
    finally:
        repository.close()
        ledger.close()


def test_lifecycle_chronology_attempted_then_complete(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=True)
        events = sorted(
            watchlist.activity(opportunity_id=seeded.opportunity_id),
            key=lambda event: (event.occurred_at, event.event_id),
        )
        types = [event.event_type for event in events]
        assert LifecycleEventType.TRIGGER_CROSSED in types
        assert LifecycleEventType.PAPER_FILL_ATTEMPTED in types
        assert LifecycleEventType.PAPER_FILL_COMPLETE in types
        assert LifecycleEventType.TRIGGER_LOST_BEFORE_FILL not in types
        crossed = types.index(LifecycleEventType.TRIGGER_CROSSED)
        attempted = types.index(LifecycleEventType.PAPER_FILL_ATTEMPTED)
        complete = types.index(LifecycleEventType.PAPER_FILL_COMPLETE)
        assert crossed < attempted < complete
        trade = ops.list_active_trades()[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.places_orders is False
    finally:
        repository.close()
        ledger.close()


def test_normal_watchlist_freshness_ages_out_without_entry_attempt(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=False)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
        assert seeded.status is OpportunityStatus.TRIGGERED
        aged = watchlist.triggered(as_of=T1 + timedelta(seconds=3), limit=10)
        assert aged == []
        row = watchlist.repository.get(seeded.opportunity_id)
        assert row is not None
        assert row.status is OpportunityStatus.REJECTED
        assert "stale_quote" in row.rejection_reasons
        assert ops.list_active_trades() == []
        events = watchlist.activity(opportunity_id=seeded.opportunity_id)
        assert any(event.event_type is LifecycleEventType.TRIGGER_LOST_BEFORE_FILL for event in events)
        assert not any(event.event_type is LifecycleEventType.PAPER_FILL_ATTEMPTED for event in events)
    finally:
        repository.close()
        ledger.close()


def test_accepted_paper_fill_keeps_execution_disabled(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=300)
        _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=True)
        trade = ops.list_active_trades()[0]
        assert settings.sports_hedge_execution_enabled is False
        assert settings.sports_hedge_mode == "paper"
        assert trade.places_orders is False
        assert trade.paper_only is True
        for cls in (MatchbookClient, PolymarketClient, KalshiClient):
            assert not hasattr(cls, "place_order")
            assert not hasattr(cls, "cancel_order")
            assert not hasattr(cls, "sign")
    finally:
        repository.close()
        ledger.close()
