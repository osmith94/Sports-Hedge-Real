"""Economic radar status stays independent of execution freshness.

Paper-only: stale/unknown quotes still cannot create a paper fill. Data class:
deterministic watchlist observations and paper-scan fixtures, not live quotes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.arbitrage.watchlist.economics import (
    NET_PROXIMITY_BAND_PP,
    classify_status,
    quote_is_execution_fresh,
)
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.paper.entry_freshness import (
    MARKET_REVALIDATION_FAILED,
    SNAPSHOT_STALE_AT_DECISION,
    UNKNOWN_QUOTE_AGE,
    snapshot_freshness_rejection,
)
from test_near_arbitrage_watchlist import EDGE_080, OBSERVED, _observation
from test_paper_entry_snapshot_freshness import (
    T1,
    _freshness_bundle,
    _observe,
    _qualify,
)


def test_classify_status_keeps_positive_edge_triggered_when_quote_is_stale() -> None:
    status, reasons = classify_status(
        _observation(
            edge=Decimal("0.015"),
            eligible=True,
            quote_age_ms=5000,
            solver_is_arbitrage=True,
            rejection_reasons=["stale_quote"],
        ),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert status is OpportunityStatus.TRIGGERED
    assert "stale_quote" in reasons
    assert quote_is_execution_fresh(5000, 2000) is False


def test_classify_status_keeps_near_when_quote_age_is_unknown() -> None:
    status, reasons = classify_status(
        _observation(quote_age_ms=None, edge=EDGE_080),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert status is OpportunityStatus.APPROACHING
    assert "unknown_quote_age" not in reasons
    assert quote_is_execution_fresh(None, 2000) is False


def test_classify_status_still_rejects_costs_depth_and_semantics() -> None:
    missing_costs, cost_reasons = classify_status(
        _observation(
            edge=Decimal("0.02"),
            eligible=True,
            quote_age_ms=80,
            rejection_reasons=["missing_fx_rate:USD"],
        ),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert missing_costs is OpportunityStatus.REJECTED
    assert "missing_fx_rate:USD" in cost_reasons

    missing_depth, depth_reasons = classify_status(
        _observation(
            rejection_reasons=["missing_executable_outcome_depth"],
            solver_is_arbitrage=False,
            quote_age_ms=80,
        ),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert missing_depth is OpportunityStatus.REJECTED
    assert "missing_executable_outcome_depth" in depth_reasons

    semantics, semantic_reasons = classify_status(
        _observation(
            edge=Decimal("0.02"),
            eligible=True,
            quote_age_ms=80,
            rejection_reasons=["settlement_mismatch"],
        ),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert semantics is OpportunityStatus.REJECTED
    assert "settlement_mismatch" in semantic_reasons


def test_tracked_read_does_not_mutate_stored_lifecycle_when_quotes_age() -> None:
    service = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=1000)
    seeded = service.observe(
        _observation(
            edge=Decimal("0.0171"),
            eligible=True,
            quote_age_ms=120,
            guaranteed_profit_gbp=Decimal("1.10"),
        )
    )
    assert seeded.status is OpportunityStatus.TRIGGERED
    aged = OBSERVED + timedelta(seconds=31)
    assert service.triggered(as_of=aged, limit=10) == []
    tracked = service.tracked(as_of=aged, limit=10)
    assert len(tracked) == 1
    assert tracked[0].status is OpportunityStatus.TRIGGERED
    assert tracked[0].is_arbitrage is True
    persisted = service.repository.get(seeded.opportunity_id)
    assert persisted is not None
    assert persisted.status is OpportunityStatus.TRIGGERED
    assert persisted.quote_age_ms == 120
    assert "stale_quote" not in persisted.rejection_reasons
    types = [event.event_type for event in service.activity(opportunity_id=seeded.opportunity_id)]
    assert LifecycleEventType.REJECTED_STALE_QUOTE not in types
    assert LifecycleEventType.TRIGGER_LOST_BEFORE_FILL not in types


def test_below_break_even_stays_watching_when_old() -> None:
    service = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=1000)
    seeded = service.observe(
        _observation(edge=Decimal("-0.004"), quote_age_ms=120, solver_is_arbitrage=False)
    )
    assert seeded.status is OpportunityStatus.WATCHING
    aged = OBSERVED + timedelta(seconds=31)
    assert service.top_near(as_of=aged, limit=10) == []
    tracked = service.tracked(as_of=aged, limit=10)
    assert tracked[0].status is OpportunityStatus.WATCHING
    persisted = service.repository.get(seeded.opportunity_id)
    assert persisted is not None
    assert persisted.status is OpportunityStatus.WATCHING
    assert persisted.current_net_edge == Decimal("-0.004")


def test_fresh_reobservation_restores_executable_triggered_without_stale_residue() -> None:
    service = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=1000, clock=lambda: OBSERVED)
    first = service.observe(
        _observation(
            edge=Decimal("0.015"),
            eligible=True,
            quote_age_ms=120,
            guaranteed_profit_gbp=Decimal("1.10"),
        )
    )
    aged = OBSERVED + timedelta(seconds=5)
    assert service.triggered(as_of=aged, limit=10) == []
    persisted = service.repository.get(first.opportunity_id)
    assert persisted is not None
    assert persisted.status is OpportunityStatus.TRIGGERED

    later = aged + timedelta(seconds=1)
    restored = service.observe(
        _observation(
            edge=Decimal("0.016"),
            eligible=True,
            quote_age_ms=90,
            observed_at=later,
            guaranteed_profit_gbp=Decimal("1.20"),
        )
    )
    assert restored.status is OpportunityStatus.TRIGGERED
    assert restored.quote_age_ms == 90
    assert "stale_quote" not in restored.rejection_reasons
    executable = service.triggered(as_of=later, limit=10)
    assert [item.opportunity_id for item in executable] == [first.opportunity_id]
    assert executable[0].status is OpportunityStatus.TRIGGERED


def test_paper_entry_still_fail_closed_on_stale_and_unknown_quotes() -> None:
    assert snapshot_freshness_rejection(
        quote_age_at_decision_ms=2000,
        simulated_latency_ms=0,
        paper_entry_max_quote_age_ms=2000,
    ) == SNAPSHOT_STALE_AT_DECISION
    assert snapshot_freshness_rejection(
        quote_age_at_decision_ms=None,
        simulated_latency_ms=0,
        paper_entry_max_quote_age_ms=2000,
    ) == UNKNOWN_QUOTE_AGE


def test_stale_watchlist_row_cannot_paper_fill(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=False)
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        assert seeded.status is OpportunityStatus.TRIGGERED
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
        dispatch_at = T1 + timedelta(milliseconds=1800)
        assert watchlist.triggered(as_of=dispatch_at, limit=10) == []
        persisted = watchlist.repository.get(seeded.opportunity_id)
        assert persisted is not None
        assert persisted.status is OpportunityStatus.TRIGGERED
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
        assert not any(event.event_type is LifecycleEventType.TRIGGER_LOST_BEFORE_FILL for event in events)
        after = watchlist.repository.get(seeded.opportunity_id)
        assert after is not None
        assert after.status is not OpportunityStatus.FILLED
        assert after.status is not OpportunityStatus.PARTIAL
    finally:
        repository.close()
        ledger.close()
