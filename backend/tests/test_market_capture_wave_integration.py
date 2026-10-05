"""Combined market-capture wave checks.

Fixture/demo books only. Proves the reviewed workstreams still agree after
merge: post-kickoff IN PLAY, execution reprice, MLB PAPER admission, and
structurally equivalent Polymarket football pairs inside the Approved Register.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_execution_reprice_before_paper_entry import (
    ENTRY_ORDER,
    _book,
    _events,
    _fresh,
    _locks,
    _market,
    _open_trade,
    _RecordingScan,
    _ScriptKalshi,
    _ScriptMatchbook,
    _stale,
)
from test_issue200_universe_hot_promotion import _market_row
from test_issue316_catalogue_registry import _costs, _fx
from test_issue344_price_engine import NOW, _engine, _row
from test_mlb_stage1 import _moneyline_markets
from test_phase3a_catalogue_native_identity import (
    EVENT_ID,
    _kalshi,
    _mb,
    _pair,
    _persist,
    _polymarket,
)

from sports_hedge.api import paper as paper_api
from sports_hedge.application.approved_market_catalogue import derived_price_engine_working_set
from sports_hedge.application.catalogue_maintenance import CATALOGUE_NATIVE_IDENTITY_CONFLICT
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.executable_liquidity import decision_net_edge
from sports_hedge.application.execution_reprice import execution_reprice_permitted
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.scan_lanes import (
    DEFAULT_BACKGROUND_INTERVAL_SECONDS,
    DEFAULT_HOT_INTERVAL_SECONDS,
    DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
    DEFAULT_UNIVERSE_INTERVAL_SECONDS,
    EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF,
    HOT_REASON_IN_PLAY,
    STARTUP_UNIVERSE_PENDING,
    ScanLane,
)
from sports_hedge.application.target_competitions import (
    TargetCompetitionCode,
    operator_competition_catalog,
)
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.catalogue.admission import catalogue_allows_live_execution
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import (
    APPROVED_PAPER_VENUE_PAIR,
    registered_canonical_key,
)
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.mlb.constants import CANONICAL_MLB_GAME_WINNER
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

KICKOFF = NOW - timedelta(minutes=25)
EVENT = "evt-inplay"
MARKET = "316199"
TICKER = "KXEPLBTTS-RICH-BTTS"
TERMINAL = {"completed", "finished", "closed", "graded", "settled"}


def _post_kickoff_fixture(
    *,
    when=NOW,
    equivalents: int | None = 5,
    evaluation: str = "evaluated",
    reason: str | None = None,
    in_running: bool | None = False,
    fixture_status: str | None = "open",
) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=f"src-{EVENT}",
        canonical_event_id=EVENT,
        home_team="Arsenal",
        away_team="Chelsea",
        competition="Premier League",
        kickoff_utc=KICKOFF,
        last_seen_at=when,
        in_running=in_running,
        fixture_status=fixture_status,
        fixture_status_source=VenueName.MATCHBOOK if fixture_status else None,
        matchbook_matched=True,
        kalshi_matched=True,
        market_evaluation_state=evaluation,
        market_evaluation_reason=reason,
        matched_equivalent_count=equivalents,
        opportunity_state="matched" if evaluation == "evaluated" else "not_evaluated",
    )


def _report(fixture: DiscoveredFixture, *, when, markets: list | None = None) -> CollectionReport:
    return CollectionReport(
        started_at=when,
        completed_at=when,
        discovered_fixtures=[fixture],
        fixture_markets={fixture.canonical_event_id: list(markets or [])},
        scan_lane=ScanLane.HOT.value,
        fixture_identity_aliases={
            fixture.canonical_event_id: fixture.canonical_event_id,
            fixture.source_event_id: fixture.canonical_event_id,
        },
    )


def _inventory(store: FixtureCurrentStateStore, when=NOW) -> dict[str, DiscoveredFixture]:
    return {item.canonical_event_id: item for item in store.inventory(when)}


@pytest.mark.asyncio
async def test_post_kickoff_in_play_stale_discovery_fills_from_execution_reprice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case A: green IN PLAY and a PAPER fill that uses the second price."""

    settings = Settings(paper_autofill_enabled=True)
    assert settings.sports_hedge_execution_enabled is False
    repository = SqliteMarketIntelligenceRepository()
    scan = _RecordingScan(
        MarketIntelligenceService(repository),
        settings=settings,
        liquidity=SqlitePaperLiquidityRepository(
            tmp_path / "inplay-liquidity.sqlite",
            matchbook_gbp=Decimal(5000),
            polymarket_usd=Decimal(5000),
            kalshi_usd=Decimal(5000),
        ),
    )
    ledger = SqlitePaperLedger(
        tmp_path / "inplay-paper.sqlite",
        seed_gbp=Decimal(5000),
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    watchlist = WatchlistService(
        SqliteWatchlistRepository(tmp_path / "inplay.watch"),
        max_quote_age_ms=10_000,
    )
    operations = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )

    def operations_factory(watchlist_arg=None, alerts=None):
        del alerts
        if watchlist_arg is not None:
            operations.watchlist = watchlist_arg
        operations.settings = settings
        return operations

    monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
    store = FixtureCurrentStateStore()
    seeded = _post_kickoff_fixture()
    store.upsert_from_report(_report(seeded, when=NOW), scan_lane=ScanLane.HOT, now=NOW)
    row = _row(
        suffix="inplay",
        kickoff=KICKOFF,
        matchbook_event_id="8899",
        matchbook_market_id=MARKET,
        kalshi_event="KXEPLBTTS-RICH",
    )
    matchbook = _ScriptMatchbook([_stale(_market()), _fresh(_market())])
    kalshi = _ScriptKalshi([_book("0.20", "0.70"), _book("0.32", "0.60")])
    engine, _, _, _layer = _engine(
        [row],
        matchbook=matchbook,
        kalshi=kalshi,
        paper_scan=scan,
        clock=FakeClock(NOW),
        fixture_state=store,
    )
    engine.venue_costs = _costs()
    engine.fx_snapshots = _fx()
    runtime = next(iter(engine._items.values()))
    assert runtime.priority is PriceEnginePriority.HOT
    paper_api.bind_price_engine_item_persist(
        engine,
        service=scan,
        audit=SqlitePaperScanRepository(tmp_path / "inplay-audit.sqlite"),
        watchlist=watchlist,
    )
    try:
        await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        await engine.drain_item_captures()
        await engine.observability.drain()
        displayed = _inventory(store)[EVENT]
        assert displayed.scan_lane == ScanLane.HOT.value
        assert HOT_REASON_IN_PLAY in (displayed.hot_reasons or [])
        assert (displayed.matched_equivalent_count or 0) > 0
        assert displayed.in_running is not True
        assert displayed.fixture_status not in TERMINAL
        assert seeded.in_running is False
        assert store.tombstone_for(EVENT) is None
        assert engine.classify_priority(runtime.identity) is PriceEnginePriority.HOT
        discovery, execution = scan.seen
        assert discovery.eligible_for_paper_simulation is False
        assert "stale_quote" in discovery.rejection_reasons
        assert execution.eligible_for_paper_simulation is True
        assert decision_net_edge(execution) != decision_net_edge(discovery)
        assert matchbook.list_events_calls == 0
        assert kalshi.list_events_calls == 0
        assert matchbook.list_markets_calls == []
        assert kalshi.list_markets_calls == []
        assert len(matchbook.get_market_calls) == 3
        assert kalshi.book_calls == [TICKER, TICKER, TICKER]
        trade = _open_trade(
            type("Bundle", (), {"operations": operations})()
        )
        assert trade.paper_only is True
        assert trade.places_orders is False
        assert trade.entry_risk is not None
        assert trade.entry_risk.net_edge == decision_net_edge(execution)
        assert trade.entry_risk.net_edge != decision_net_edge(discovery)
        events = _events(watchlist, trade.opportunity_id)
        types = [event.event_type for event in events]
        assert [item for item in types if item in ENTRY_ORDER] == list(ENTRY_ORDER)
        qualifying = next(
            event for event in events if event.event_type is LifecycleEventType.QUALIFYING_DETECTED
        )
        paper_eligible = next(
            event for event in events if event.event_type is LifecycleEventType.PAPER_ELIGIBLE
        )
        assert qualifying.current_net_edge == decision_net_edge(discovery)
        assert paper_eligible.current_net_edge == decision_net_edge(execution)
        assert _locks(type("Bundle", (), {"ledger": ledger})())[0] > 0
    finally:
        repository.close()
        ledger.close()


def test_zero_equivalent_closure_leaves_hot_without_a_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case B: a finished empty book leaves HOT and does not start a fill."""

    captures: list[object] = []
    monkeypatch.setattr(
        paper_api,
        "persist_price_engine_item_capture",
        lambda *args, **kwargs: captures.append((args, kwargs)),
    )
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(_post_kickoff_fixture(equivalents=5), when=NOW),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    later = NOW + timedelta(seconds=30)
    closed = _post_kickoff_fixture(when=later, equivalents=0, in_running=None)
    store.upsert_from_report(_report(closed, when=later), scan_lane=ScanLane.HOT, now=later)
    assert store.inventory(later) == []
    assert store.hot_identity_scope(later) == []
    tombstone = store.tombstone_for(EVENT)
    assert tombstone is not None
    assert tombstone.reason == EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF
    assert tombstone.provider_status not in TERMINAL
    assert closed.fixture_status == "open"
    assert closed.in_running is None
    engine, _, _, _layer = _engine(
        [
            _row(
                suffix="inplay",
                kickoff=KICKOFF,
                matchbook_event_id="8899",
                matchbook_market_id=MARKET,
                kalshi_event="KXEPLBTTS-RICH",
            )
        ],
        clock=FakeClock(later),
        fixture_state=store,
    )
    runtime = next(iter(engine._items.values()))
    assert engine.classify_priority(runtime.identity) is PriceEnginePriority.BACKGROUND
    assert engine.due_items(PriceEnginePriority.HOT, now=later) == []
    assert captures == []


def test_incomplete_post_kickoff_refresh_keeps_in_play_and_hot_priority() -> None:
    """Case C: a failed refresh is not a zero-equivalent closure."""

    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(_post_kickoff_fixture(), when=NOW, markets=[_market_row(arb=False, edge=None)]),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    later = NOW + timedelta(seconds=20)
    failed = _post_kickoff_fixture(
        when=later,
        evaluation="evaluated",
        reason="provider_timeout",
        equivalents=0,
        in_running=False,
    )
    store.upsert_from_report(_report(failed, when=later), scan_lane=ScanLane.HOT, now=later)
    row = _inventory(store, later)[EVENT]
    assert HOT_REASON_IN_PLAY in (row.hot_reasons or [])
    assert row.fixture_status == "open"
    assert row.in_running is False
    assert store.tombstone_for(EVENT) is None
    engine, _, _, _layer = _engine(
        [
            _row(
                suffix="inplay",
                kickoff=KICKOFF,
                matchbook_event_id="8899",
                matchbook_market_id=MARKET,
                kalshi_event="KXEPLBTTS-RICH",
            )
        ],
        clock=FakeClock(later),
        fixture_state=store,
    )
    runtime = next(iter(engine._items.values()))
    assert engine.classify_priority(runtime.identity) is PriceEnginePriority.HOT
    assert engine.due_items(PriceEnginePriority.HOT, now=later)


def test_mlb_registered_admission_reprices_without_enabling_execution() -> None:
    """Case D: MLB registered pairs enter the funnel. Execution stays disabled."""

    kalshi, polymarket, matchbook = _moneyline_markets()
    assert registered_canonical_key(kalshi, matchbook) == CANONICAL_MLB_GAME_WINNER
    assert registered_canonical_key(kalshi, polymarket) == CANONICAL_MLB_GAME_WINNER
    matched = MarketMatcher().match(kalshi, matchbook)
    assert scan_eligible_pair(kalshi, matchbook, matched) is True
    assessment = classify_pair(kalshi, matchbook)
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert catalogue_allows_live_execution(kalshi, matchbook) is True
    assert catalogue_allows_live_execution(kalshi, polymarket) is True
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    catalog = {item["code"]: item for item in operator_competition_catalog()}
    assert catalog[TargetCompetitionCode.MLB.value]["paper_executable"] is True
    from test_execution_reprice_before_paper_entry import _decision

    stale = _decision()
    assert stale.eligible_for_paper_simulation is False
    assert execution_reprice_permitted(stale) is True


def test_football_polymarket_structural_pairs_are_paper_admitted() -> None:
    """Case E: structurally equivalent football PM pairs are PAPER-admitted."""

    assert APPROVED_PAPER_VENUE_PAIR == frozenset(
        {VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET}
    )
    assert registered_canonical_key(_mb(), _kalshi()) == "MATCH_RESULT_FT"
    assert registered_canonical_key(_mb(), _polymarket()) == "MATCH_RESULT_FT"
    assert registered_canonical_key(_polymarket(), _kalshi()) == "MATCH_RESULT_FT"
    catalog = {item["code"]: item for item in operator_competition_catalog()}
    assert catalog[TargetCompetitionCode.PREMIER_LEAGUE.value]["paper_executable"] is True
    assert Settings().sports_hedge_execution_enabled is False


def test_native_id_conflict_cannot_replace_execution_exact_ids() -> None:
    """Case F: a conflicting native id is not what execution reprice would fetch."""

    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(store, [_pair(matchbook=_mb(), kalshi=_kalshi())])
        original = store.list_active()[0]
        conflicts = []
        _persist(
            store,
            [
                _pair(
                    kalshi=_kalshi("KXUEFANLGAME-OTHER"),
                    polymarket=_polymarket(event_id="999", market_id="888"),
                    kalshi_event="KXUEFANLGAME-OTHER",
                )
            ],
            conflicts,
        )
        assert conflicts
        assert conflicts[0].reason == CATALOGUE_NATIVE_IDENTITY_CONFLICT
        held = store.get_row(original.catalogue_row_id)
        assert held is not None
        assert held.kalshi_event_ticker == "KXUEFANLGAME-26SEP26ENGESP"
        assert held.matchbook_market_id == "mb-1x2"
        assert held.polymarket_event_id is None
        identity = derived_price_engine_working_set(store.list_active())[0]
        assert identity.canonical_event_id == EVENT_ID
        assert identity.kalshi_event_ticker == "KXUEFANLGAME-26SEP26ENGESP"
        assert "KXUEFANLGAME-OTHER" not in identity.kalshi_market_tickers
        engine, _, _, _layer = _engine([], fixture_state=FixtureCurrentStateStore())
        assert engine._execution_identity_ready(
            identity, (VenueName.MATCHBOOK, VenueName.KALSHI)
        )
        poisoned = identity.model_copy(update={"kalshi_event_ticker": ""})
        assert (
            engine._execution_identity_ready(
                poisoned, (VenueName.MATCHBOOK, VenueName.KALSHI)
            )
            is False
        )
    finally:
        store.close()


def test_scanner_operating_limits_are_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"
    assert Settings.model_fields["paper_scan_matchbook_concurrency"].default == 4
    assert Settings.model_fields["paper_scan_kalshi_concurrency"].default == 4
    assert Settings.model_fields["paper_scan_polymarket_concurrency"].default == 8
    assert Settings.model_fields["min_net_edge"].default == 0.01
    assert Settings.model_fields["paper_active_trade_interval_seconds"].default == 5
    assert Settings.model_fields["paper_live_refresh_universe_interval_seconds"].default == 180
    assert DEFAULT_HOT_INTERVAL_SECONDS == 30
    assert DEFAULT_BACKGROUND_INTERVAL_SECONDS == 600
    assert DEFAULT_UNIVERSE_INTERVAL_SECONDS == 180
    assert DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING == timedelta(hours=4)
    assert STARTUP_UNIVERSE_PENDING == "startup_universe_pending"
