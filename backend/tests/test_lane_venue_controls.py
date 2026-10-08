from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_paper_trade_lifecycle import _ops
from test_read_only_collector import FakeMatchbook, FakePolymarket
from test_venue_union_discovery import NewcastleKalshi
from venue_cost_helpers import matchbook_polymarket_costs

from sports_hedge.api.main import app
from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.lane_venues import (
    INSUFFICIENT_VENUES_WARNING,
    MALFORMED_VENUE_SETTINGS_WARNING,
    VENUE_HEALTH_DISABLED,
    coerce_operator_venues,
    comparison_allowed,
    default_operator_venues,
)
from sports_hedge.application.live_refresh import (
    LiveRefreshCoordinator,
    get_live_refresh_coordinator,
)
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.lane_venue_settings import (
    SqliteLaneVenueSettingsStore,
    get_lane_venue_settings_store,
    resolve_lane_venue_participation,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
FAR = NOW + timedelta(days=3)


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


class CountingKalshi(NewcastleKalshi):
    def __init__(self) -> None:
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []
        self.book_calls = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        self.list_events_calls += 1
        return await super().list_events(**filters)

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        self.list_markets_calls.append(str(event_id))
        return await super().list_markets(event_id, **filters)

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        self.book_calls += 1
        return await super().get_order_book(event_id, market_id, outcome_id, **filters)


def _paper_scan() -> tuple[PaperScanService, SqliteMarketIntelligenceRepository]:
    repository = SqliteMarketIntelligenceRepository()
    return PaperScanService(MarketIntelligenceService(repository)), repository


def _collector(
    matchbook: Any,
    polymarket: Any,
    kalshi: Any | None = None,
) -> ReadOnlyCrossVenueCollector:
    scan, _repository = _paper_scan()
    return ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=scan,
    )


def _fixture(canonical_id: str, *, kickoff: datetime, source: VenueName = VenueName.POLYMARKET) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=source,
        source_event_id=canonical_id,
        canonical_event_id=canonical_id,
        home_team="Home",
        away_team="Away",
        competition="Premier League",
        kickoff_utc=kickoff,
        last_seen_at=NOW,
        polymarket_matched=source is VenueName.POLYMARKET,
        matchbook_matched=source is VenueName.MATCHBOOK,
        market_evaluation_state="evaluated",
        opportunity_state="matched",
    )


def _report(
    fixtures: list[DiscoveredFixture],
    *,
    lane: str,
    when: datetime = NOW,
    source_venue: VenueName = VenueName.POLYMARKET,
) -> CollectionReport:
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=[],
        discovered_fixtures=fixtures,
        scan_lane=lane,
        enabled_venues=[source_venue, VenueName.MATCHBOOK],
        fixture_source_events={
            item.canonical_event_id: [
                {
                    "venue": source_venue.value,
                    "source_event_id": item.source_event_id,
                    "raw": {"id": item.source_event_id, "name": "Home vs Away"},
                }
            ]
            for item in fixtures
        },
        fixture_identity_aliases={
            item.canonical_event_id: item.canonical_event_id for item in fixtures
        },
    )


def test_coerce_keeps_operator_venues_and_allows_empty() -> None:
    assert coerce_operator_venues(["polymarket", "matchbook", "smarkets", "polymarket"]) == (
        VenueName.POLYMARKET,
        VenueName.MATCHBOOK,
    )
    assert coerce_operator_venues([]) == ()
    assert comparison_allowed([]) is False
    assert comparison_allowed(default_operator_venues()) is True


@pytest.mark.asyncio
async def test_disabled_polymarket_receives_zero_universe_calls() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    kalshi = CountingKalshi()
    collector = _collector(matchbook, polymarket, kalshi)
    report = await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        scan_lane=ScanLane.UNIVERSE.value,
    )
    assert polymarket.list_events_calls == 0
    assert polymarket.list_markets_calls == []
    assert polymarket.book_calls == []
    assert matchbook.list_events_calls == 1
    assert kalshi.list_events_calls == 1
    assert report.venue_health["polymarket"] == VENUE_HEALTH_DISABLED
    assert report.enabled_venues == [VenueName.MATCHBOOK, VenueName.KALSHI]
    assert VenueName.POLYMARKET not in report.matching_venues


@pytest.mark.asyncio
async def test_disabled_kalshi_receives_zero_universe_calls() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    kalshi = CountingKalshi()
    kalshi.get_market_calls = []

    async def get_market(ticker: str, **_filters: Any) -> dict[str, Any]:
        kalshi.get_market_calls.append(str(ticker))
        raise AssertionError("disabled Kalshi must not get_market")

    kalshi.get_market = get_market  # type: ignore[method-assign]
    collector = _collector(matchbook, polymarket, kalshi)
    report = await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        scan_lane=ScanLane.UNIVERSE.value,
    )
    assert kalshi.list_events_calls == 0
    assert kalshi.list_markets_calls == []
    assert kalshi.book_calls == 0
    assert kalshi.get_market_calls == []
    assert matchbook.list_events_calls == 1
    assert polymarket.list_events_calls == 1
    assert report.venue_health["kalshi"] == VENUE_HEALTH_DISABLED
    assert VenueName.KALSHI not in report.matching_venues
    assert report.enabled_venues == [VenueName.MATCHBOOK, VenueName.POLYMARKET]


@pytest.mark.asyncio
async def test_disabled_matchbook_receives_zero_universe_calls() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    kalshi = CountingKalshi()
    collector = _collector(matchbook, polymarket, kalshi)
    report = await collector.collect_and_scan(
        enabled_venues=[VenueName.POLYMARKET, VenueName.KALSHI],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        scan_lane=ScanLane.UNIVERSE.value,
    )
    assert matchbook.list_events_calls == 0
    assert matchbook.list_markets_calls == []
    assert polymarket.list_events_calls == 1
    assert kalshi.list_events_calls == 1
    assert report.venue_health["matchbook"] == VENUE_HEALTH_DISABLED


@pytest.mark.asyncio
async def test_hot_skip_discovery_does_not_call_disabled_polymarket() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    kalshi = CountingKalshi()
    collector = _collector(matchbook, polymarket, kalshi)
    known = {
        "cluster-1": [
            {
                "venue": "matchbook",
                "source_event_id": "1001",
                "raw": {
                    "id": 1001,
                    "name": "Newcastle United vs Chelsea",
                    "start": datetime(2026, 9, 20, 15, 0, tzinfo=UTC).isoformat(),
                    "competition-name": "Premier League",
                },
            },
            {
                "venue": "polymarket",
                "source_event_id": "pm-event-1",
                "raw": {
                    "id": "pm-event-1",
                    "title": "Newcastle United vs Chelsea",
                    "startTime": datetime(2026, 9, 20, 15, 0, tzinfo=UTC).isoformat(),
                    "competition": "Premier League",
                },
            },
            {
                "venue": "kalshi",
                "source_event_id": "KXEPLGAME-26SEP20NEWCHE",
                "raw": {
                    "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
                    "series_ticker": "KXEPLGAME",
                    "title": "Newcastle United vs Chelsea",
                    "category": "Sports",
                    "strike_date": datetime(2026, 9, 20, 15, 0, tzinfo=UTC).isoformat(),
                },
            },
        ]
    }
    report = await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        scan_lane=ScanLane.HOT.value,
        identity_scope=["cluster-1"],
        known_source_events=known,
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
    )
    assert polymarket.list_events_calls == 0
    assert polymarket.list_markets_calls == []
    assert polymarket.book_calls == []
    assert matchbook.list_events_calls == 0
    assert report.raw_polymarket_events == 0
    assert report.venue_health["polymarket"] == VENUE_HEALTH_DISABLED


@pytest.mark.asyncio
async def test_fast_and_full_venue_settings_are_independent() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    kalshi = CountingKalshi()
    collector = _collector(matchbook, polymarket, kalshi)
    await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        scan_lane=ScanLane.HOT.value,
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
    )
    assert polymarket.list_events_calls == 0
    await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI],
        scan_lane=ScanLane.UNIVERSE.value,
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
    )
    assert polymarket.list_events_calls >= 1
    assert polymarket.list_markets_calls


@pytest.mark.asyncio
async def test_fewer_than_two_venues_warns_and_does_not_invent_comparisons() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    collector = _collector(matchbook, polymarket, CountingKalshi())
    report = await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
    )
    assert INSUFFICIENT_VENUES_WARNING in report.config_warnings
    assert report.paper_decisions == []
    assert report.matched_market_pairs == 0
    assert polymarket.list_events_calls == 0
    assert matchbook.list_events_calls == 1


def test_operator_selection_persists_across_store_restart(tmp_path: Path) -> None:
    database = tmp_path / "paper_settings.sqlite"
    store = SqliteLaneVenueSettingsStore(database)
    saved = store.save(
        [VenueName.MATCHBOOK, VenueName.KALSHI],
        [VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI],
    )
    assert saved.source == "operator"
    assert saved.hot == [VenueName.MATCHBOOK, VenueName.KALSHI]
    store.close()
    restarted = SqliteLaneVenueSettingsStore(database)
    loaded = restarted.load()
    assert loaded is not None
    assert loaded.hot == [VenueName.MATCHBOOK, VenueName.KALSHI]
    assert loaded.universe == [VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI]
    restarted.close()


def test_hot_cycle_does_not_delete_universe_polymarket_state() -> None:
    store = FixtureCurrentStateStore()
    universe_fixture = _fixture("pm-weekend", kickoff=FAR, source=VenueName.POLYMARKET)
    store.upsert_from_report(
        _report([universe_fixture], lane=ScanLane.UNIVERSE.value, source_venue=VenueName.POLYMARKET),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    hot_fixture = _fixture("mb-live", kickoff=NOW, source=VenueName.MATCHBOOK)
    store.upsert_from_report(
        _report([hot_fixture], lane=ScanLane.HOT.value, source_venue=VenueName.MATCHBOOK),
        scan_lane=ScanLane.HOT,
        now=NOW + timedelta(seconds=5),
    )
    record = store._rows["pm-weekend"]
    assert record.universe is not None
    assert any(event.venue is VenueName.POLYMARKET for event in record.source_events())
    assert "pm-weekend" in store.known_source_events(["pm-weekend"])
    assert store.known_source_events(["pm-weekend"])["pm-weekend"][0]["venue"] == "polymarket"


def test_same_fixture_disabled_venue_keeps_prior_source_events() -> None:
    store = FixtureCurrentStateStore()
    shared = _fixture("shared-fx", kickoff=FAR, source=VenueName.POLYMARKET)
    store.upsert_from_report(
        _report([shared], lane=ScanLane.UNIVERSE.value, source_venue=VenueName.POLYMARKET),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    hot_only_mb = _report(
        [_fixture("shared-fx", kickoff=FAR, source=VenueName.MATCHBOOK)],
        lane=ScanLane.HOT.value,
        source_venue=VenueName.MATCHBOOK,
    )
    store.upsert_from_report(hot_only_mb, scan_lane=ScanLane.HOT, now=NOW + timedelta(seconds=5))
    venues_after_hot = {
        event["venue"] for event in store.known_source_events(["shared-fx"])["shared-fx"]
    }
    assert venues_after_hot == {"polymarket", "matchbook"}

    universe_without_pm = _report(
        [_fixture("shared-fx", kickoff=FAR, source=VenueName.MATCHBOOK)],
        lane=ScanLane.UNIVERSE.value,
        source_venue=VenueName.MATCHBOOK,
    )
    store.upsert_from_report(
        universe_without_pm,
        scan_lane=ScanLane.UNIVERSE,
        now=NOW + timedelta(seconds=10),
    )
    venues_after_universe = {
        event["venue"] for event in store.known_source_events(["shared-fx"])["shared-fx"]
    }
    assert "polymarket" in venues_after_universe
    assert "matchbook" in venues_after_universe


def test_env_defaults_keep_all_three_venues_when_no_operator_row() -> None:
    store = SqliteLaneVenueSettingsStore(":memory:")
    loaded = resolve_lane_venue_participation(store)
    assert loaded.source == "env_default"
    assert loaded.hot == list(default_operator_venues())
    assert loaded.universe == list(default_operator_venues())
    assert VenueName.POLYMARKET in loaded.hot
    store.close()


def test_in_flight_cycle_keeps_snapshot_when_operator_toggles() -> None:
    store = SqliteLaneVenueSettingsStore(":memory:")
    coordinator = LiveRefreshCoordinator(venue_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.apply_venue_participation(
        default_operator_venues(),
        default_operator_venues(),
    )
    coordinator._mark_lane_started(ScanLane.HOT, NOW)
    snapshot = coordinator.running_cycle_venues()
    assert VenueName.POLYMARKET in snapshot
    coordinator.apply_venue_participation(
        [VenueName.MATCHBOOK, VenueName.KALSHI],
        default_operator_venues(),
    )
    assert VenueName.POLYMARKET in coordinator.running_cycle_venues()
    assert VenueName.POLYMARKET not in coordinator.pending_venues_for(ScanLane.HOT)
    assert coordinator.status.hot.applies_next_cycle is True
    assert VenueName.POLYMARKET in coordinator.pending_venues_for(ScanLane.UNIVERSE)


def test_disabled_polymarket_cannot_auto_capture(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        decision = ops._plans[opportunity_id].decision
        assert any(leg.venue is VenueName.KALSHI for leg in decision.fill_legs)
        called: list[int] = []

        def _blocked(*_args: Any, **_kwargs: Any) -> None:
            called.append(1)
            raise PaperOperationsError("must_not_auto_capture")

        ops.simulate_fill = _blocked  # type: ignore[method-assign]
        ops.persist_triggered_chain(
            decision,
            autofill=True,
            refreshed_venues=(VenueName.MATCHBOOK, VenueName.POLYMARKET),
        )
        assert called == []
        ops.persist_triggered_chain(
            decision,
            autofill=True,
            refreshed_venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
        )
        assert called == [1]
    finally:
        repository.close()
        ledger.close()


def test_venue_participation_http_persists_and_is_lane_specific(tmp_path: Path) -> None:
    store = SqliteLaneVenueSettingsStore(tmp_path / "paper_settings.sqlite")
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator.bind_venue_store(store)
    client = TestClient(app)
    try:
        body = {
            "hot": {"matchbook": True, "kalshi": True, "polymarket": False},
            "universe": {"matchbook": True, "kalshi": True, "polymarket": True},
        }
        saved = client.put("/paper/venue-participation", json=body).json()
        assert saved["hot"]["pending_venues"] == ["matchbook", "kalshi"]
        assert saved["universe"]["pending_venues"] == ["matchbook", "polymarket", "kalshi"]
        assert saved["hot"]["comparison_ready"] is True
        too_few = client.put(
            "/paper/venue-participation",
            json={
                "hot": {"matchbook": True, "kalshi": False, "polymarket": False},
                "universe": {"matchbook": True, "kalshi": True, "polymarket": True},
            },
        ).json()
        assert too_few["hot"]["comparison_ready"] is False
        assert too_few["hot"]["venue_warning"]
        again = client.get("/paper/live-refresh").json()
        assert again["hot"]["pending_venues"] == ["matchbook"]
        assert again["universe"]["pending_venues"] == ["matchbook", "polymarket", "kalshi"]
        restarted = SqliteLaneVenueSettingsStore(tmp_path / "paper_settings.sqlite")
        loaded = restarted.load()
        assert loaded is not None
        assert loaded.hot == [VenueName.MATCHBOOK]
        restarted.close()
    finally:
        coordinator._venue_store = None
        get_lane_venue_settings_store.cache_clear()
        coordinator.reset()
        store.close()


@pytest.mark.asyncio
async def test_all_venues_off_does_not_invent_matchbook_matching_venue() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    kalshi = CountingKalshi()
    collector = _collector(matchbook, polymarket, kalshi)
    report = await collector.collect_and_scan(
        enabled_venues=[],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        scan_lane=ScanLane.UNIVERSE.value,
    )
    assert report.enabled_venues == []
    assert report.matching_venues == []
    assert report.matching_venue is None
    assert report.venue_health == {
        "matchbook": VENUE_HEALTH_DISABLED,
        "polymarket": VENUE_HEALTH_DISABLED,
        "kalshi": VENUE_HEALTH_DISABLED,
    }
    assert INSUFFICIENT_VENUES_WARNING in report.config_warnings
    assert report.paper_decisions == []
    assert report.matched_market_pairs == 0
    assert matchbook.list_events_calls == 0
    assert polymarket.list_events_calls == 0
    assert kalshi.list_events_calls == 0

    coordinator = LiveRefreshCoordinator(venue_settings_store=SqliteLaneVenueSettingsStore(":memory:"))
    coordinator.record_explicit_report(report)
    assert coordinator.status.matching_venue is None
    assert coordinator.status.matching_venues == []
    assert coordinator.status.venue_health == report.venue_health
    coordinator.apply_venue_participation([], [])
    assert coordinator.status.hot.pending_venues == []
    assert coordinator.status.universe.pending_venues == []
    assert coordinator.status.hot.comparison_ready is False
    assert coordinator.status.universe.comparison_ready is False
    assert coordinator.status.hot.venue_warning
    assert coordinator.status.universe.venue_warning


def test_malformed_operator_row_does_not_silently_reenable_venues(tmp_path: Path) -> None:
    database = tmp_path / "paper_settings.sqlite"
    store = SqliteLaneVenueSettingsStore(database)
    store.save(
        [VenueName.MATCHBOOK, VenueName.KALSHI],
        [VenueName.MATCHBOOK, VenueName.KALSHI],
    )
    store.close()
    sidecar = sqlite3.connect(database)
    sidecar.execute(
        """
        UPDATE lane_venue_participation
        SET hot_venues_json = ?, universe_venues_json = ?
        WHERE id = 1
        """,
        ("not-json", '{"venues": true}'),
    )
    sidecar.commit()
    sidecar.close()
    restarted = SqliteLaneVenueSettingsStore(database)
    loaded = resolve_lane_venue_participation(restarted)
    assert loaded.hot == []
    assert loaded.universe == []
    assert VenueName.POLYMARKET not in loaded.hot
    assert VenueName.MATCHBOOK not in loaded.hot
    assert loaded.source == "operator"
    assert loaded.config_diagnostic == MALFORMED_VENUE_SETTINGS_WARNING
    restarted.close()

    empty_valid = tmp_path / "empty_valid.sqlite"
    valid_store = SqliteLaneVenueSettingsStore(empty_valid)
    valid_store.save([], [])
    valid_loaded = valid_store.load()
    assert valid_loaded is not None
    assert valid_loaded.hot == []
    assert valid_loaded.universe == []
    assert valid_loaded.config_diagnostic is None
    valid_store.close()


def test_concurrent_read_write_restart_keeps_last_durable_settings(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-settings.sqlite"
    store = SqliteLaneVenueSettingsStore(path)
    errors: list[Exception] = []
    combos = (
        ([VenueName.MATCHBOOK], [VenueName.MATCHBOOK, VenueName.KALSHI]),
        ([VenueName.MATCHBOOK, VenueName.KALSHI], list(default_operator_venues())),
        ([], [VenueName.POLYMARKET, VenueName.KALSHI]),
        ([VenueName.POLYMARKET], [VenueName.MATCHBOOK]),
    )

    def write(index: int) -> None:
        hot, universe = combos[index % len(combos)]
        store.save(hot, universe)

    def read() -> None:
        store.load()
        resolve_lane_venue_participation(store)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(write, index) for index in range(24)]
            futures.extend(pool.submit(read) for _ in range(24))
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
        assert errors == []
        expected = store.save(
            [VenueName.MATCHBOOK, VenueName.KALSHI],
            list(default_operator_venues()),
        )
        store.close()
        restarted = SqliteLaneVenueSettingsStore(path)
        loaded = restarted.load()
        assert loaded is not None
        assert loaded.hot == expected.hot
        assert loaded.universe == expected.universe
        assert loaded.source == "operator"
        restarted.close()
    finally:
        store.close()

