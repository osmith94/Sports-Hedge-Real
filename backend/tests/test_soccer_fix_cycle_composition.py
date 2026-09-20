"""Soccer fix-cycle composition: overlapping #396/#399/#400/#403/#404 coexist.

Semantic union proofs only. PAPER / read-only. No venue writes.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_settlement_agent import PaperSettlementAgent
from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.allocation.models import AllocationConstraintKind
from sports_hedge.arbitrage.allocation.policy import policy_from_settings
from sports_hedge.arbitrage.watchlist.models import (
    OPERATOR_ACTIVITY_EVENT_TYPES,
    LifecycleEventType,
    WatchLeg,
    WatchObservation,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.football import FootballPeriod, MarketFamily, format_stored_line
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.result_resolution import PAPER_AUTO_SETTLEMENT_SOURCE
from sports_hedge.paper.trades import PaperTrade, PaperTradeAuditEventType, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_paper_auto_settlement import (
    FakeKalshi,
    FakeMatchbook,
    _graded_event,
    _graded_matchbook_btts,
    _kalshi_settled,
)
from test_paper_trade_lifecycle import _ops
from test_step8c_bankroll_allocator import (
    _assert_not_open_count_rejection,
    _count_only_opens,
    _demo_request,
    _fill_high,
)


NOW = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
LINE = Decimal("2.5")


def _legs() -> list[WatchLeg]:
    return [
        WatchLeg(
            outcome="over",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-total",
            currency="GBP",
            native_stake=Decimal("50"),
            gbp_per_unit=Decimal("1"),
            gbp_stake=Decimal("50"),
            net_decimal_odds=Decimal("1.95"),
            cumulative_depth_gbp=Decimal("80"),
        ),
        WatchLeg(
            outcome="under",
            venue=VenueName.KALSHI,
            source_market_id="ks-total",
            currency="USD",
            native_stake=Decimal("40") / Decimal("0.75"),
            gbp_per_unit=Decimal("0.75"),
            gbp_stake=Decimal("40"),
            net_decimal_odds=Decimal("2.05"),
            cumulative_depth_gbp=Decimal("60"),
        ),
    ]


def test_watchlist_persists_canonical_line_and_capture_eligibility_together() -> None:
    """#399 line + #396 capture/fixture metadata share one schema extension."""

    extras_src = inspect.getsource(SqliteWatchlistRepository._create_schema)
    assert '"line": "TEXT"' in extras_src
    assert '"capture_eligible": "INTEGER"' in extras_src
    upsert_src = inspect.getsource(SqliteWatchlistRepository._upsert_opportunity_locked)
    assert upsert_src.count("?") == 49

    service = WatchlistService(SqliteWatchlistRepository())
    stored = service.observe(
        WatchObservation(
            observed_at=NOW,
            canonical_event_id="evt-compose",
            canonical_market_id="mkt-total-2-5",
            settlement_key="regulation_time|full_time",
            competition="Premier League",
            home_team="Arsenal",
            away_team="Chelsea",
            market_family=MarketFamily.TOTAL_GOALS,
            period=FootballPeriod.FULL_TIME,
            line=LINE,
            legs=_legs(),
            trigger_net_edge=Decimal("0.01"),
            current_net_edge=Decimal("0.012"),
            implied_probability_sum=Decimal("1") / Decimal("1.012"),
            solver_is_arbitrage=True,
            eligible_for_paper_simulation=True,
            quote_age_ms=120,
            limiting_depth_gbp=Decimal("60"),
            capital_required_gbp=Decimal("90"),
            guaranteed_profit_gbp=Decimal("1.10"),
        )
    )
    assert stored.line == LINE
    assert stored.capture_eligible is True
    reloaded = service.repository.get(stored.opportunity_id)
    assert reloaded is not None
    assert reloaded.line == LINE
    assert reloaded.capture_eligible is True
    first_seen = service.activity(opportunity_id=stored.opportunity_id)[-1]
    assert first_seen.fixture_label == "Arsenal v Chelsea"
    assert first_seen.market_family == "total_goals"
    assert first_seen.capture_eligible is True


def test_composed_settings_keep_settlement_cadence_and_ignore_open_count_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#403 retired count cap + #404 settlement interval on one Settings object."""

    fields = Settings.model_fields
    assert "paper_settlement_interval_seconds" in fields
    assert "allocation_max_concurrent_open" not in fields
    monkeypatch.setenv("ALLOCATION_MAX_CONCURRENT_OPEN", "1")
    settings = Settings()
    assert not hasattr(settings, "allocation_max_concurrent_open")
    assert settings.paper_settlement_interval_seconds == 30
    policy = policy_from_settings(settings)
    assert "max_concurrent_open_opportunities" not in type(policy).model_fields
    result = allocate(
        _demo_request(open_positions=_count_only_opens(10), policy=policy, fill=_fill_high())
    )
    assert result.accepted is True
    _assert_not_open_count_rejection(result)
    assert result.limiting_constraint is not AllocationConstraintKind.CONCURRENCY


def test_paper_trade_schema_keeps_line_and_settlement_blocked() -> None:
    """#399 canonical line and #404 settlement-blocked audit share PaperTrade."""

    assert "line" in PaperTrade.model_fields
    assert PaperTradeAuditEventType.SETTLEMENT_BLOCKED.value == "settlement_blocked"
    src = inspect.getsource(PaperOperationsService.record_active_lifecycle_event)
    assert "format_stored_line(trade.line)" in src
    settle_src = inspect.getsource(PaperOperationsService.settle)
    assert "self.watchlist.close(" in settle_src
    agent_src = inspect.getsource(PaperSettlementAgent._settle)
    assert "self.operations.settle(" in agent_src
    assert "place_order" not in agent_src
    assert "cancel_order" not in agent_src


@pytest.mark.asyncio
async def test_auto_settlement_keeps_line_and_emits_existing_trade_exited(
    tmp_path: Path,
) -> None:
    """#404 settle() path + #399 line persistence + #395 operator CLOSED card."""

    ledger = SqlitePaperLedger(tmp_path / "compose-paper.sqlite")
    _scan, watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        trade.line = LINE
        ops.trades.save(trade)
        opportunity = watchlist.repository.get(trade.opportunity_id)
        assert opportunity is not None
        before_operator = {
            event.event_type for event in watchlist.operator_activity(opportunity_id=trade.opportunity_id)
        }
        assert LifecycleEventType.PAPER_FILL_COMPLETE in before_operator
        assert LifecycleEventType.CLOSED not in before_operator

        matchbook = FakeMatchbook(_graded_matchbook_btts(winner_id=301), _graded_event(home=1, away=1))
        kalshi_ticker = next(
            leg.source_contract_id or leg.source_market_id
            for leg in trade.legs
            if leg.venue is VenueName.KALSHI
        )
        kalshi = FakeKalshi({str(kalshi_ticker): _kalshi_settled(result="yes")})
        agent = PaperSettlementAgent(
            operations=ops,
            matchbook=matchbook,
            kalshi=kalshi,
            provider_access=None,
            clock=lambda: NOW,
        )
        cycle = await agent.run_cycle(now=NOW)
        assert trade.trade_id in cycle.settled_trade_ids

        closed = ops.trades.get(trade.trade_id)
        assert closed is not None
        assert closed.state is PaperTradeState.CLOSED
        assert closed.line == LINE
        assert format_stored_line(closed.line) == "2.5"
        assert closed.settlement_source == PAPER_AUTO_SETTLEMENT_SOURCE
        journal = ops.query_active_trade_events(trade_id=trade.trade_id)
        settled_events = [event for event in journal if event.event_type.value == "settled"]
        assert settled_events
        assert settled_events[0].line == "2.5"

        operator = watchlist.operator_activity(opportunity_id=trade.opportunity_id)
        operator_types = [event.event_type for event in operator]
        assert LifecycleEventType.CLOSED in operator_types
        assert LifecycleEventType.PAPER_FILL_COMPLETE in operator_types
        closed_events = [event for event in operator if event.event_type is LifecycleEventType.CLOSED]
        assert len(closed_events) == 1
        assert closed_events[0].detail == "paper settlement"
        assert set(operator_types) <= OPERATOR_ACTIVITY_EVENT_TYPES
        audit_types = {event.event_type for event in watchlist.activity(opportunity_id=trade.opportunity_id)}
        assert LifecycleEventType.PAPER_FILL_ATTEMPTED in audit_types
        assert LifecycleEventType.PAPER_FILL_ATTEMPTED not in set(operator_types)
    finally:
        repository.close()
        ledger.close()


def test_composed_sources_stay_paper_only() -> None:
    """Composition must not grow venue mutation or kickoff-inferred settlement."""

    import sports_hedge.paper.result_resolution as result_resolution
    from sports_hedge.application.live_refresh import LiveRefreshCoordinator

    settle_src = inspect.getsource(PaperSettlementAgent)
    resolution_src = inspect.getsource(result_resolution.resolve_paper_trade_settlement)
    for blob in (settle_src, resolution_src):
        assert "place_order" not in blob
        assert "cancel_order" not in blob
        assert "sign_wallet" not in blob
    assert result_resolution.__doc__ is not None
    assert "elapsed kickoff time" in result_resolution.__doc__
    live_src = inspect.getsource(LiveRefreshCoordinator._maybe_run_paper_settlement)
    assert "create_task" not in live_src
    assert "paper_settlement_interval_seconds" in live_src
