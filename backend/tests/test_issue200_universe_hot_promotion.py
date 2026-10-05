"""Issue #200: UNIVERSE qualifying arbs promote into HOT identity.

Data class: deterministic fixture/demo current-state and paper-scan payloads.
Not live, historical, or modelled venue quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_read_only_collector import FakeMatchbook, FakePolymarket
from test_step8f_automatic_paper_entry import (
    _matchbook_btts,
    _observe_and_persist,
    _ops_bundle,
    _kalshi_btts,
    _kalshi_costs,
)

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    InventoryPairResult,
    VenueMarketFacts,
    VenueQuoteFact,
)
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import (
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_HOT_INTERVAL_SECONDS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_UNIVERSE_INTERVAL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    ScanLane,
    classify_scan_lane,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import PaperTradeState

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
DISTANT_KICKOFF = NOW + timedelta(days=3)
NEAR_KICKOFF = NOW + timedelta(minutes=45)
CANONICAL_ID = "t3d-qualifying"


class CountingMatchbook(FakeMatchbook):
    def __init__(self) -> None:
        super().__init__()
        self.list_events_calls = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        self.list_events_calls += 1
        return await super().list_events(**filters)


class CountingPolymarket(FakePolymarket):
    def __init__(self) -> None:
        super().__init__()
        self.list_events_calls = 0

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        self.list_events_calls += 1
        return await super().list_events(**filters)


def _facts(
    venue: VenueName,
    *,
    source_market_id: str,
    quote_age_ms: int | None = 80,
    settlement_key: str | None = "regulation_time|full_time",
    settlement_complete: bool | None = True,
) -> VenueMarketFacts:
    return VenueMarketFacts(
        venue=venue,
        source_event_id=f"{venue.value}-{CANONICAL_ID}",
        source_market_id=source_market_id,
        family="both_teams_to_score",
        period="full_time",
        settlement_key=settlement_key,
        settlement_complete=settlement_complete,
        best_backs=[
            VenueQuoteFact(outcome="yes", decimal_odds=Decimal("2.10"), size_at_touch=Decimal(100)),
            VenueQuoteFact(outcome="no", decimal_odds=Decimal("1.80"), size_at_touch=Decimal(100)),
        ],
        quote_age_ms=quote_age_ms,
        quote_age_basis="source",
        native_currency="GBP" if venue is VenueName.MATCHBOOK else "USD",
    )


def _market_row(
    *,
    status: InventoryComparisonStatus = InventoryComparisonStatus.MATCHED_EQUIVALENT,
    edge: Decimal | None = Decimal("0.015"),
    arb: bool = True,
    quote_age_ms: int | None = 80,
    reason: str | None = None,
    rejection_reasons: list[str] | None = None,
    entered_solver: bool | None = None,
    settlement_key: str | None = "regulation_time|full_time",
    settlement_complete: bool | None = True,
    trigger: Decimal | None = Decimal("0.01"),
    limiting_depth_gbp: Decimal | None = Decimal("80"),
) -> FixtureMarketInventoryRow:
    equivalent = status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    reasons = list(rejection_reasons or [])
    if not equivalent and reason and reason not in reasons:
        reasons.append(reason)
    return FixtureMarketInventoryRow(
        display_name="BTTS",
        family="both_teams_to_score",
        period="full_time",
        comparison_status=status,
        reason=reason,
        rejection_reasons=reasons,
        match_reasons=[],
        entered_solver=equivalent if entered_solver is None else entered_solver,
        solver_model="strict_complete_set" if (equivalent if entered_solver is None else entered_solver) else None,
        current_net_edge=edge,
        trigger_net_edge=trigger,
        limiting_depth_gbp=limiting_depth_gbp,
        distance_to_trigger_pp=None if edge is None or trigger is None else trigger - edge,
        solver_is_arbitrage=arb,
        matchbook=_facts(
            VenueName.MATCHBOOK,
            source_market_id="mb-btts",
            quote_age_ms=quote_age_ms,
            settlement_key=settlement_key,
            settlement_complete=settlement_complete,
        ),
        polymarket=_facts(
            VenueName.POLYMARKET,
            source_market_id="pm-btts",
            quote_age_ms=quote_age_ms,
            settlement_key=settlement_key,
            settlement_complete=settlement_complete,
        ),
        pair_results=[
            InventoryPairResult(
                left_venue=VenueName.MATCHBOOK,
                right_venue=VenueName.POLYMARKET,
                entered_solver=equivalent if entered_solver is None else entered_solver,
                solver_model="strict_complete_set",
                current_net_edge=edge,
                rejection_reasons=reasons,
                solver_is_arbitrage=arb,
            )
        ],
    )


def _fixture(
    canonical_id: str = CANONICAL_ID,
    *,
    kickoff: datetime = DISTANT_KICKOFF,
    evaluation: str = "evaluated",
    opportunity: str = "matched",
    arb: bool = False,
    qualifying: int = 0,
    when: datetime = NOW,
    in_running: bool | None = None,
    fixture_status: str | None = None,
) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=canonical_id,
        canonical_event_id=canonical_id,
        home_team="Home",
        away_team="Away",
        competition="Premier League",
        kickoff_utc=kickoff,
        last_seen_at=when,
        in_running=in_running,
        fixture_status=fixture_status,
        matchbook_matched=True,
        polymarket_matched=True,
        qualifying_market_count=qualifying,
        solver_is_arbitrage=arb,
        opportunity_state=opportunity,
        market_evaluation_state=evaluation,
    )


def _decision(event_id: str, market_id: str, *, when: datetime = NOW) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_event_id=event_id,
        canonical_market_id=market_id,
        fixture_canonical_event_id=event_id,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
        scanned_at=when,
        eligible_for_paper_simulation=False,
    )


def _report(
    fixtures: list[DiscoveredFixture],
    *,
    when: datetime = NOW,
    scan_lane: str = ScanLane.UNIVERSE.value,
    markets: dict[str, list[FixtureMarketInventoryRow]] | None = None,
    decisions: list[PaperScanDecision] | None = None,
) -> CollectionReport:
    paper = decisions or [
        _decision(item.canonical_event_id, f"mkt-{item.canonical_event_id}", when=when)
        for item in fixtures
        if item.market_evaluation_state == "evaluated"
    ]
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=paper,
        discovered_fixtures=fixtures,
        fixture_markets=markets or {},
        scan_lane=scan_lane,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        operator_summary="issue-200",
        fixture_identity_aliases={
            item.canonical_event_id: item.canonical_event_id for item in fixtures
        },
        fixture_source_events={
            item.canonical_event_id: [
                {
                    "venue": "matchbook",
                    "source_event_id": item.source_event_id,
                    "raw": {
                        "id": item.source_event_id,
                        "name": "Home vs Away",
                        "start": item.kickoff_utc.isoformat(),
                        "competition-name": "Premier League",
                    },
                },
                {
                    "venue": "polymarket",
                    "source_event_id": f"pm-{item.source_event_id}",
                    "raw": {
                        "id": f"pm-{item.source_event_id}",
                        "title": "Home vs Away",
                        "startTime": item.kickoff_utc.isoformat(),
                        "competition": "Premier League",
                    },
                },
            ]
            for item in fixtures
        },
    )


def _qualifying_universe_report(
    canonical_id: str = CANONICAL_ID,
    *,
    kickoff: datetime = DISTANT_KICKOFF,
    when: datetime = NOW,
    row: FixtureMarketInventoryRow | None = None,
) -> CollectionReport:
    fixture = _fixture(
        canonical_id,
        kickoff=kickoff,
        when=when,
        arb=True,
        qualifying=1,
        opportunity="qualifying",
    )
    market = row or _market_row()
    return _report(
        [fixture],
        when=when,
        markets={canonical_id: [market]},
        decisions=[_decision(canonical_id, f"mkt-{canonical_id}", when=when)],
    )


def test_lifecycle_classifier_stays_universe_for_distant_fixture() -> None:
    fixture = _fixture()
    assert classify_scan_lane(fixture, NOW) is ScanLane.UNIVERSE
    assert classify_scan_lane(fixture, NOW + timedelta(seconds=30)) is ScanLane.UNIVERSE


def test_distant_universe_qualifying_arb_enters_next_hot_identity_scope() -> None:
    """Reproduce Issue #200: qualifying T+3d UNIVERSE arb must join the next HOT scope."""

    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    coordinator.record_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE)

    store = coordinator.fixture_current_state()
    assert store.detail(CANONICAL_ID) is not None

    clock.advance(30)
    plan = coordinator.plan_tick(now=clock.now)
    assert plan.lane == "hot"
    assert CANONICAL_ID in plan.identity_scope
    assert CANONICAL_ID in store.hot_identity_scope(clock.now)
    assert CANONICAL_ID in plan.known_source_events
    assert plan.known_source_events[CANONICAL_ID]


def test_distant_equivalent_nonqualifying_promotes_surveillance_not_paper() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    near_edge = _market_row(edge=Decimal("0.008"), arb=False, trigger=Decimal("0.01"))
    fixture = _fixture(opportunity="near", arb=False, qualifying=0)
    coordinator.record_report(
        _report(
            [fixture],
            markets={CANONICAL_ID: [near_edge]},
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    store = coordinator.fixture_current_state()
    assert CANONICAL_ID in store.hot_identity_scope(NOW)
    later = NOW + timedelta(seconds=30)
    coordinator._next_hot_due = NOW
    plan = coordinator.plan_tick(now=later)
    assert plan.lane == "hot"
    assert CANONICAL_ID in plan.identity_scope
    inventory = store.inventory(later)
    row = next(item for item in inventory if item.canonical_event_id == CANONICAL_ID)
    assert any(str(reason).startswith("NET PROXIMITY") for reason in (row.hot_reasons or []))
    assert "ARB PROMOTION" not in (row.hot_reasons or [])
    assert "SURVEILLANCE" not in (row.hot_reasons or [])


def test_ui_label_without_current_state_markets_does_not_promote() -> None:
    store = FixtureCurrentStateStore()
    labelled = _fixture(arb=True, qualifying=1, opportunity="qualifying")
    store.upsert_from_report(_report([labelled]), scan_lane=ScanLane.UNIVERSE, now=NOW)
    assert labelled.canonical_event_id not in store.hot_identity_scope(NOW)


def test_unknown_settlement_cannot_cause_promotion() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(
        status=InventoryComparisonStatus.SETTLEMENT_MISMATCH,
        reason="unknown_settlement_scope",
        rejection_reasons=["unknown_settlement_scope", "incomplete_settlement"],
        arb=True,
        entered_solver=False,
        settlement_key=None,
        settlement_complete=None,
    )
    store.upsert_from_report(
        _qualifying_universe_report(row=row),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert CANONICAL_ID not in store.hot_identity_scope(NOW + timedelta(seconds=30))


def test_stale_quotes_can_enter_surveillance_hot() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(quote_age_ms=50_000, arb=True, rejection_reasons=["stale_quote"])
    store.upsert_from_report(
        _qualifying_universe_report(row=row),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert CANONICAL_ID in store.hot_identity_scope(NOW + timedelta(seconds=30))


def test_non_executable_depth_fees_fx_risk_allocator_can_enter_surveillance_hot() -> None:
    store = FixtureCurrentStateStore()
    cases = [
        ["missing_executable_outcome_depth"],
        ["insufficient_depth"],
        ["missing_costs"],
        ["missing_fx_rate"],
        ["execution_risk_above_threshold"],
        ["allocator_size_required"],
        ["passive_maker_not_executable"],
    ]
    for reasons in cases:
        row = _market_row(arb=True, rejection_reasons=reasons)
        store.clear()
        store.upsert_from_report(
            _qualifying_universe_report(row=row),
            scan_lane=ScanLane.UNIVERSE,
            now=NOW,
        )
        assert CANONICAL_ID in store.hot_identity_scope(NOW + timedelta(seconds=30)), reasons


def test_promoted_fixture_leaves_hot_when_no_longer_qualifying() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    later = NOW + timedelta(seconds=30)
    assert CANONICAL_ID in store.hot_identity_scope(later)

    cooled = _fixture(opportunity="matched", arb=False, qualifying=0, when=later)
    cooled_row = _market_row(edge=Decimal("0"), arb=False)
    store.upsert_from_report(
        _report(
            [cooled],
            when=later,
            scan_lane=ScanLane.HOT.value,
            markets={CANONICAL_ID: [cooled_row]},
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert CANONICAL_ID not in store.hot_identity_scope(later)
    hot, universe = store.membership_counts(later)
    assert hot == 0
    assert universe == 1


def test_lifecycle_hot_survives_lost_opportunity_promotion() -> None:
    store = FixtureCurrentStateStore()
    near = _fixture(
        "t45m",
        kickoff=NEAR_KICKOFF,
        arb=True,
        qualifying=1,
        opportunity="qualifying",
    )
    store.upsert_from_report(
        _report(
            [near],
            markets={"t45m": [_market_row()]},
            decisions=[_decision("t45m", "mkt-t45m")],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert "t45m" in store.hot_identity_scope(NOW)
    later = NOW + timedelta(seconds=30)
    store.upsert_from_report(
        _report(
            [_fixture("t45m", kickoff=NEAR_KICKOFF, opportunity="near", when=later)],
            when=later,
            scan_lane=ScanLane.HOT.value,
            markets={"t45m": [_market_row(edge=Decimal("0"), arb=False)]},
            decisions=[_decision("t45m", "mkt-t45m", when=later)],
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert "t45m" in store.hot_identity_scope(later)
    assert classify_scan_lane(near, later) is ScanLane.HOT


def test_cadence_and_radar_ttl_remain_distinct_from_executable_freshness() -> None:
    assert DEFAULT_HOT_INTERVAL_SECONDS == 30
    assert DEFAULT_UNIVERSE_INTERVAL_SECONDS == 180
    assert DEFAULT_HOT_TTL_SECONDS == 90
    assert DEFAULT_UNIVERSE_TTL_SECONDS == 360
    assert DEFAULT_EXECUTABLE_QUOTE_AGE_MS == 1000

    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    still_radar = NOW + timedelta(seconds=30)
    assert still_radar - NOW > timedelta(milliseconds=DEFAULT_EXECUTABLE_QUOTE_AGE_MS)
    assert CANONICAL_ID in store.hot_identity_scope(still_radar)
    expired = NOW + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    assert CANONICAL_ID not in store.hot_identity_scope(expired)


@pytest.mark.asyncio
async def test_hot_uses_known_source_events_and_skips_universe_discovery() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    coordinator.record_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE)
    clock.advance(30)
    plan = coordinator.plan_tick(now=clock.now)
    assert plan.lane == "hot"
    assert plan.identity_scope == [CANONICAL_ID]
    assert CANONICAL_ID in plan.known_source_events

    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        cycle_timeout_seconds=5,
    )
    try:
        report = await collector.collect_and_scan(
            scan_lane=plan.lane,
            identity_scope=plan.identity_scope,
            known_source_events=plan.known_source_events,
            cycle_timeout_seconds=plan.collector_timeout_seconds,
            max_event_pairs=8,
        )
        assert matchbook.list_events_calls == 0
        assert polymarket.list_events_calls == 0
        assert report.scan_lane == ScanLane.HOT.value
    finally:
        repository.close()


def test_universe_qualifying_persists_immediately_and_hot_refresh_is_idempotent(
    tmp_path: Path,
) -> None:
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
        first_trades = ops.list_active_trades()
        assert len(first_trades) == 1
        first = first_trades[0]
        assert first.state is PaperTradeState.OPEN
        first_journals = list(ops.journal.list_entries())
        first_snap = ledger.treasury.snapshot()

        clock = FakeClock(NOW)
        coordinator = LiveRefreshCoordinator(clock=clock)
        coordinator.reset()
        coordinator._clock = clock
        distant_id = decision.canonical_event_id or CANONICAL_ID
        fixture = _fixture(
            distant_id,
            kickoff=DISTANT_KICKOFF,
            arb=True,
            qualifying=1,
            opportunity="qualifying",
        )
        universe_report = _report(
            [fixture],
            markets={distant_id: [_market_row()]},
            decisions=[decision],
        )
        coordinator.record_report(universe_report, scan_lane=ScanLane.UNIVERSE)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        assert len(ops.list_active_trades()) == 1
        assert ops.list_active_trades()[0].trade_id == first.trade_id

        clock.advance(30)
        plan = coordinator.plan_tick(now=clock.now)
        assert plan.lane == "hot"
        assert distant_id in plan.identity_scope

        hot_report = universe_report.model_copy(
            update={"scan_lane": ScanLane.HOT.value, "completed_at": clock.now, "started_at": clock.now}
        )
        coordinator.record_report(hot_report, scan_lane=ScanLane.HOT)
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
    finally:
        repository.close()
        ledger.close()
