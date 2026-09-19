"""Issue #264: snapshot-bound paper-entry freshness.

Paper fill ages the qualified snapshot (T1) by configured simulated latency
only. Backend dispatch delay (T2 − T1) is telemetry. Data class: deterministic
fixture/demo paper-scan payloads, not live venue quotes. Phase 1 remains
paper-only / read-only toward venues.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.executable_liquidity import DEFAULT_OPENING_MAX_QUOTE_AGE_MS
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import (
    ORPHANED_PAPER_FILLING_RECONCILED,
    LifecycleEventType,
    OpportunityStatus,
    PaperFillAttemptStatus,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.entry_freshness import (
    MARKET_REVALIDATION_FAILED,
    SNAPSHOT_STALE_AT_DECISION,
    SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL,
    assess_paper_entry_freshness,
    conservative_quote_age_at_decision_ms,
    snapshot_freshness_rejection,
)
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient
from test_step8f_automatic_paper_entry import (
    FX,
    _assert_allocator_sized,
    _matchbook_btts,
    _kalshi_btts,
    _kalshi_costs,
    _standing,
)

T1 = datetime(2026, 9, 17, 17, 0, 0, 300000, tzinfo=UTC)


def _freshness_bundle(
    tmp_path: Path,
    *,
    autofill: bool,
    latency_ms: int = 500,
    max_age_ms: int = 2000,
    watchlist_max_age_ms: int | None = None,
    watchlist_db: Path | None = None,
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
        SqliteWatchlistRepository(watchlist_db or ":memory:"),
        max_quote_age_ms=watchlist_max_age_ms if watchlist_max_age_ms is not None else max_age_ms,
    )
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    return scan, watchlist, ops, repository, ledger, settings


def _with_quote_age(
    decision,
    age_ms: int,
    *,
    scanned_at: datetime = T1,
    processing_delay_ms: int = 0,
):
    captured = scanned_at - timedelta(milliseconds=processing_delay_ms)
    legs = [
        leg.model_copy(update={"quote_age_ms": age_ms, "quote_captured_at": captured})
        for leg in decision.fill_legs
    ]
    return decision.model_copy(
        update={"quote_age_ms": age_ms, "fill_legs": legs, "scanned_at": scanned_at}
    )


def _qualify(
    scan,
    *,
    age_ms: int,
    scanned_at: datetime = T1,
    processing_delay_ms: int = 0,
):
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
    return _with_quote_age(
        decision, age_ms, scanned_at=scanned_at, processing_delay_ms=processing_delay_ms
    )


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
        snapshot_bound=True,
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


def test_conservative_quote_age_includes_capture_to_decision_elapsed() -> None:
    captured = T1 - timedelta(milliseconds=400)
    age = conservative_quote_age_at_decision_ms(
        decision_at=T1,
        known_age_ms=300,
        quote_captured_at=captured,
    )
    assert age == 700
    freshness = assess_paper_entry_freshness(
        decision_at=T1,
        dispatched_at=T1 + timedelta(milliseconds=1500),
        quote_age_at_decision_ms=300,
        simulated_latency_ms=500,
        paper_entry_max_quote_age_ms=2000,
        quote_captured_at=captured,
        snapshot_bound=True,
    )
    assert freshness.quote_age_at_decision_ms == 700
    assert freshness.simulated_arrival_quote_age_ms == 1200
    assert freshness.accepted is True


def test_backend_delay_does_not_create_false_stale_rejection(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        assert seeded.status is OpportunityStatus.TRIGGERED
        dispatch_at = T1 + timedelta(milliseconds=1500)
        presented = watchlist.triggered(as_of=dispatch_at, limit=10)
        assert [row.opportunity_id for row in presented] == [seeded.opportunity_id]
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
            now=dispatch_at,
        )
        trades = ops.list_active_trades()
        assert len(trades) == 1
        plan = ops._plans[seeded.opportunity_id]
        freshness = plan.entry_freshness
        assert freshness is not None
        assert freshness.simulated_arrival_quote_age_ms == 800
        assert freshness.decision_to_autofill_dispatch_ms == 1500
        assert freshness.quote_age_at_decision_ms == 300
        assert freshness.simulated_latency_ms == 500
        assert freshness.paper_entry_max_quote_age_ms == 2000
        assert freshness.accepted is True
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
        watchlist.begin_paper_fill_attempt(
            seeded.opportunity_id,
            occurred_at=T1,
            bind_snapshot=True,
        )
        dispatch_at = T1 + timedelta(milliseconds=1800)
        aged = watchlist.triggered(as_of=dispatch_at, limit=10)
        assert aged == []
        row = watchlist.repository.get(seeded.opportunity_id)
        assert row is not None
        assert row.status is OpportunityStatus.PAPER_FILLING
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


def test_manual_fill_does_not_revive_aged_rejected_snapshot(tmp_path: Path) -> None:
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
        with pytest.raises(PaperOperationsError, match=MARKET_REVALIDATION_FAILED):
            ops.simulate_fill(
                seeded.opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
                now=dispatch_at,
            )
        assert ops.list_active_trades() == []
        events = watchlist.activity(opportunity_id=seeded.opportunity_id)
        assert not any(event.event_type is LifecycleEventType.PAPER_FILL_COMPLETE for event in events)
        assert any(event.event_type is LifecycleEventType.TRIGGER_LOST_BEFORE_FILL for event in events)
    finally:
        repository.close()
        ledger.close()


def test_genuine_stale_at_simulated_arrival_fails(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=1700)
        seeded = _observe(scan, watchlist, decision)
        assert seeded.status is OpportunityStatus.TRIGGERED
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
            now=T1 + timedelta(milliseconds=1500),
        )
        plan = ops._plans[seeded.opportunity_id]
        assert plan.entry_freshness is not None
        assert plan.entry_freshness.quote_age_at_decision_ms == 1700
        assert plan.entry_freshness.simulated_arrival_quote_age_ms == 2200
        assert plan.entry_freshness.decision_to_autofill_dispatch_ms == 1500
        assert plan.entry_freshness.rejection_reason == SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL
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


def test_processing_delay_between_capture_and_t1_counts_toward_arrival_age(
    tmp_path: Path,
) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=300, processing_delay_ms=400)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
            now=T1 + timedelta(milliseconds=1500),
        )
        plan = ops._plans[seeded.opportunity_id]
        freshness = plan.entry_freshness
        assert freshness is not None
        assert freshness.quote_age_at_decision_ms == 700
        assert freshness.simulated_arrival_quote_age_ms == 1200
        assert freshness.decision_to_autofill_dispatch_ms == 1500
        assert freshness.accepted is True
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
    finally:
        repository.close()
        ledger.close()


def test_processing_delay_can_make_simulated_arrival_stale(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=300, processing_delay_ms=1300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
            now=T1 + timedelta(milliseconds=1500),
        )
        plan = ops._plans[seeded.opportunity_id]
        freshness = plan.entry_freshness
        assert freshness is not None
        assert freshness.quote_age_at_decision_ms == 1600
        assert freshness.simulated_arrival_quote_age_ms == 2100
        assert freshness.rejection_reason == SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()


def test_rejected_attempt_does_not_suppress_later_trigger_lost(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        stale = _qualify(scan, age_ms=1700)
        seeded = _observe(scan, watchlist, stale)
        ops.persist_triggered_chain(
            stale,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
            now=T1,
        )
        assert ops.list_active_trades() == []
        assert ops._entry_rejections[seeded.opportunity_id] == SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL
        assert watchlist.has_active_bound_attempt(seeded.opportunity_id) is False

        later = T1 + timedelta(seconds=3)
        fresh = _qualify(scan, age_ms=300, scanned_at=later)
        retriggered = _observe(scan, watchlist, fresh)
        assert retriggered.status is OpportunityStatus.TRIGGERED
        lost_at = later + timedelta(seconds=3)
        aged = watchlist.triggered(as_of=lost_at, limit=10)
        assert aged == []
        row = watchlist.repository.get(seeded.opportunity_id)
        assert row is not None
        assert row.status is OpportunityStatus.REJECTED
        lost = [
            event
            for event in watchlist.activity(opportunity_id=seeded.opportunity_id)
            if event.event_type is LifecycleEventType.TRIGGER_LOST_BEFORE_FILL
            and event.occurred_at >= later
        ]
        assert lost
        assert watchlist.has_active_bound_attempt(seeded.opportunity_id) is False
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


def _timing_leg(
    *,
    venue: VenueName,
    outcome: str,
    quote_age_ms: int | None,
    quote_captured_at: datetime | None,
    requested_stake: Decimal = Decimal("10"),
) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id=f"{venue.value}-mkt",
        source_runner_id=f"{venue.value}-runner",
        requested_stake=requested_stake,
        displayed_odds=Decimal("2.10"),
        levels=[BookLevel(decimal_odds=Decimal("2.10"), available_stake=Decimal("100"))],
        quote_age_ms=quote_age_ms,
        quote_captured_at=quote_captured_at,
    )


def test_crossed_leg_quote_age_does_not_synthesize_impossible_quote() -> None:
    age = conservative_quote_age_at_decision_ms(
        decision_at=T1,
        known_age_ms=900,
        quote_captured_at=T1 - timedelta(milliseconds=1000),
        legs=[
            _timing_leg(
                venue=VenueName.MATCHBOOK,
                outcome="yes",
                quote_age_ms=100,
                quote_captured_at=T1 - timedelta(milliseconds=1000),
            ),
            _timing_leg(
                venue=VenueName.POLYMARKET,
                outcome="no",
                quote_age_ms=900,
                quote_captured_at=T1 - timedelta(milliseconds=100),
            ),
        ],
    )
    assert age == 1100


def test_oldest_provider_age_and_capture_on_same_leg_stay_conservative() -> None:
    age = conservative_quote_age_at_decision_ms(
        decision_at=T1,
        known_age_ms=50,
        quote_captured_at=T1 - timedelta(milliseconds=10),
        legs=[
            _timing_leg(
                venue=VenueName.MATCHBOOK,
                outcome="yes",
                quote_age_ms=400,
                quote_captured_at=T1 - timedelta(milliseconds=800),
            ),
            _timing_leg(
                venue=VenueName.POLYMARKET,
                outcome="no",
                quote_age_ms=100,
                quote_captured_at=T1 - timedelta(milliseconds=50),
            ),
        ],
    )
    assert age == 1200


def test_missing_timing_on_required_leg_is_unknown() -> None:
    age = conservative_quote_age_at_decision_ms(
        decision_at=T1,
        known_age_ms=100,
        quote_captured_at=T1 - timedelta(milliseconds=1000),
        legs=[
            _timing_leg(
                venue=VenueName.MATCHBOOK,
                outcome="yes",
                quote_age_ms=100,
                quote_captured_at=T1 - timedelta(milliseconds=200),
            ),
            _timing_leg(
                venue=VenueName.POLYMARKET,
                outcome="no",
                quote_age_ms=None,
                quote_captured_at=None,
            ),
        ],
    )
    assert age is None


def test_scan_pair_final_qualification_includes_allocator_delay() -> None:
    captured = datetime.now(UTC)
    scan = PaperScanService(
        MarketIntelligenceService(SqliteMarketIntelligenceRepository()),
        settings=Settings(max_slippage_bps=0, fx_spread_bps=0, simulated_latency_ms=500),
    )
    left = _matchbook_btts().model_copy(update={"observed_at": captured, "quote_age_ms": 100})
    right = _kalshi_btts().model_copy(update={"observed_at": captured, "quote_age_ms": 150})
    real_allocate = scan._allocate_draft

    def delayed_allocate(*args, **kwargs):
        time.sleep(0.65)
        return real_allocate(*args, **kwargs)

    scan._allocate_draft = delayed_allocate
    decision = scan.scan_pair(
        left,
        right,
        venue_costs=_kalshi_costs(),
        fx_snapshots=FX,
        maximum_execution_risk=100,
        liquidity_snapshot=_standing(),
    )
    assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
    qualification_ms = int((decision.scanned_at - captured).total_seconds() * 1000)
    assert qualification_ms >= 600
    age = conservative_quote_age_at_decision_ms(
        decision_at=decision.scanned_at,
        known_age_ms=decision.quote_age_ms,
        legs=decision.fill_legs,
    )
    assert age is not None
    assert age >= 750
    plan_age = PaperOperationsService(
        watchlist=WatchlistService(SqliteWatchlistRepository()),
        settings=Settings(),
    )._plan_from_decision(decision, "opp-t1", DataProvenance.LIVE_PAPER).quote_age_at_decision_ms
    assert plan_age is not None
    assert plan_age >= 750


def test_manual_paper_filling_does_not_inherit_bound_snapshot_authority(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=False)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
        watchlist.begin_paper_fill_attempt(
            seeded.opportunity_id,
            occurred_at=T1,
            bind_snapshot=False,
        )
        assert watchlist.has_active_bound_attempt(seeded.opportunity_id) is False
        attempt = watchlist.latest_paper_fill_attempt(seeded.opportunity_id)
        assert attempt is not None
        assert attempt.bound_snapshot is False
        dispatch_at = T1 + timedelta(milliseconds=1800)
        with pytest.raises(PaperOperationsError, match=MARKET_REVALIDATION_FAILED):
            ops.simulate_fill(
                seeded.opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
                now=dispatch_at,
            )
        assert ops.list_active_trades() == []
        assert watchlist.has_active_bound_attempt(seeded.opportunity_id) is False
    finally:
        repository.close()
        ledger.close()


def test_failed_attempt_then_fresh_retrigger_creates_second_attempt_events(
    tmp_path: Path,
) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        stale = _qualify(scan, age_ms=1700)
        seeded = _observe(scan, watchlist, stale)
        ops.persist_triggered_chain(stale, provenance=DataProvenance.LIVE_PAPER, autofill=True)
        first_attempt = watchlist.latest_paper_fill_attempt(seeded.opportunity_id)
        assert first_attempt is not None
        assert first_attempt.status is PaperFillAttemptStatus.REJECTED
        later = T1 + timedelta(seconds=2)
        fresh = _qualify(scan, age_ms=300, scanned_at=later)
        _observe(scan, watchlist, fresh)
        ops.persist_triggered_chain(
            fresh,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
            now=later + timedelta(milliseconds=200),
        )
        attempts = watchlist.repository.list_paper_fill_attempts(seeded.opportunity_id)
        assert len(attempts) == 2
        assert attempts[0].attempt_id != attempts[1].attempt_id
        attempted = [
            event
            for event in watchlist.activity(opportunity_id=seeded.opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_FILL_ATTEMPTED
        ]
        rejected = [
            event
            for event in watchlist.activity(opportunity_id=seeded.opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_FILL_REJECTED
        ]
        complete = [
            event
            for event in watchlist.activity(opportunity_id=seeded.opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_FILL_COMPLETE
        ]
        assert len(attempted) == 2
        assert attempted[0].event_id != attempted[1].event_id
        assert first_attempt.attempt_id in attempted[0].event_id or first_attempt.attempt_id in attempted[1].event_id
        second = next(item for item in attempts if item.attempt_id != first_attempt.attempt_id)
        assert any(second.attempt_id in event.event_id for event in attempted)
        assert any(first_attempt.attempt_id in event.event_id for event in rejected)
        assert any(second.attempt_id in event.event_id for event in complete)
        assert len(ops.list_active_trades()) == 1
    finally:
        repository.close()
        ledger.close()


def test_retry_inside_one_attempt_does_not_duplicate_attempted_event(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=True)
        first_attempt = watchlist.latest_paper_fill_attempt(seeded.opportunity_id)
        assert first_attempt is not None
        for _ in range(2):
            ops.simulate_fill(
                seeded.opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
                now=T1 + timedelta(milliseconds=200),
            )
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
        assert len(attempted) == 1
        assert first_attempt.attempt_id in attempted[0].event_id
        assert len(complete) == 1
        assert first_attempt.attempt_id in complete[0].event_id
        assert len(watchlist.repository.list_paper_fill_attempts(seeded.opportunity_id)) == 1
        assert len(ops.list_active_trades()) == 1
    finally:
        repository.close()
        ledger.close()


def test_crash_after_paper_filling_reconciles_on_restart(tmp_path: Path) -> None:
    watchlist_db = tmp_path / "watchlist.sqlite"
    scan, watchlist, ops, repository, ledger, settings = _freshness_bundle(
        tmp_path, autofill=False, watchlist_db=watchlist_db
    )
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
        watchlist.begin_paper_fill_attempt(
            seeded.opportunity_id,
            occurred_at=T1,
            bind_snapshot=True,
            decision_at=decision.scanned_at,
        )
        row = watchlist.repository.get(seeded.opportunity_id)
        assert row is not None
        assert row.status is OpportunityStatus.PAPER_FILLING
        first_attempt = watchlist.latest_paper_fill_attempt(seeded.opportunity_id)
        assert first_attempt is not None
        assert first_attempt.bound_snapshot is True
        assert first_attempt.status is PaperFillAttemptStatus.STARTED
        assert ops.list_active_trades() == []
    finally:
        watchlist.repository.close()
        repository.close()
        ledger.close()

    scan2, watchlist2, ops2, repository2, ledger2, _settings2 = _freshness_bundle(
        tmp_path, autofill=True, watchlist_db=watchlist_db
    )
    try:
        assert ops2._plans == {}
        orphan = watchlist2.repository.get(seeded.opportunity_id)
        assert orphan is not None
        assert orphan.status is OpportunityStatus.PAPER_FILLING
        later = T1 + timedelta(seconds=2)
        fresh = _qualify(scan2, age_ms=300, scanned_at=later)
        retriggered = _observe(scan2, watchlist2, fresh)
        assert retriggered.status is OpportunityStatus.PAPER_FILLING
        ops2.persist_triggered_chain(
            fresh,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
            now=later + timedelta(milliseconds=200),
        )
        recovered = watchlist2.latest_paper_fill_attempt(seeded.opportunity_id)
        first_after = watchlist2.repository.get_paper_fill_attempt(first_attempt.attempt_id)
        assert first_after is not None
        assert first_after.status is PaperFillAttemptStatus.REJECTED
        assert first_after.detail is not None
        assert ORPHANED_PAPER_FILLING_RECONCILED in first_after.detail
        assert recovered is not None
        assert recovered.attempt_id != first_attempt.attempt_id
        assert recovered.status is PaperFillAttemptStatus.COMPLETE
        trades = ops2.list_active_trades()
        assert len(trades) == 1
        assert trades[0].state is PaperTradeState.OPEN
        fill_ids = [leg.fill_id for leg in trades[0].legs if leg.fill_id]
        assert len(fill_ids) == len(set(fill_ids)) == 2
        locks = _lock_rows(ledger2, trades[0].trade_id)
        assert len(locks) == 2
        row = watchlist2.repository.get(seeded.opportunity_id)
        assert row is not None
        assert row.status is OpportunityStatus.FILLED
        assert watchlist2.has_active_bound_attempt(seeded.opportunity_id) is False
        attempted = [
            event
            for event in watchlist2.activity(opportunity_id=seeded.opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_FILL_ATTEMPTED
        ]
        assert len(attempted) == 2
        assert first_attempt.attempt_id in attempted[0].event_id or first_attempt.attempt_id in attempted[1].event_id
        assert recovered.attempt_id in attempted[0].event_id or recovered.attempt_id in attempted[1].event_id
        ops2.persist_triggered_chain(
            fresh,
            provenance=DataProvenance.LIVE_PAPER,
            autofill=True,
            now=later + timedelta(milliseconds=400),
        )
        assert len(ops2.list_active_trades()) == 1
        assert _lock_rows(ledger2, trades[0].trade_id) == locks
        assert settings.sports_hedge_execution_enabled is False
    finally:
        watchlist2.repository.close()
        repository2.close()
        ledger2.close()
