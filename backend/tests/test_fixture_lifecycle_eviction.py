"""Issue #164: current-state lifecycle eviction without deleting audit history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from venue_cost_helpers import matchbook_polymarket_costs

from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import (
    EVICTION_TERMINAL,
    EVICTION_TERMINAL_FROM_MATCHBOOK,
    MATCHBOOK_TERMINAL_EVENT_STATES,
    ScanLane,
    classify_scan_lane,
    is_matchbook_lifecycle_status,
    is_trusted_lifecycle_correction,
    should_skip_market_work,
    terminal_eviction_reason,
)
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision

NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


def _fixture(
    canonical_id: str,
    *,
    kickoff: datetime,
    in_running: bool | None = None,
    fixture_status: str | None = None,
    fixture_status_source: VenueName | None = None,
    source: VenueName = VenueName.MATCHBOOK,
    source_event_id: str | None = None,
    matchbook_matched: bool = True,
    polymarket_matched: bool = False,
    kalshi_matched: bool = False,
    evaluation: str = "evaluated",
) -> DiscoveredFixture:
    status_source = fixture_status_source
    if status_source is None and fixture_status is not None:
        status_source = source
    return DiscoveredFixture(
        source=source,
        source_event_id=source_event_id or f"src-{canonical_id}",
        canonical_event_id=canonical_id,
        home_team="Home",
        away_team="Away",
        competition="Premier League",
        kickoff_utc=kickoff,
        last_seen_at=NOW,
        in_running=in_running,
        fixture_status=fixture_status,
        fixture_status_source=status_source,
        matchbook_matched=matchbook_matched,
        polymarket_matched=polymarket_matched,
        kalshi_matched=kalshi_matched,
        market_evaluation_state=evaluation,
        opportunity_state="matched",
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
    extra_aliases: dict[str, str] | None = None,
) -> CollectionReport:
    aliases = {item.canonical_event_id: item.canonical_event_id for item in fixtures}
    for item in fixtures:
        aliases[item.source_event_id] = item.canonical_event_id
        aliases[f"pair-{item.canonical_event_id}"] = item.canonical_event_id
    if extra_aliases:
        aliases.update(extra_aliases)
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=[
            _decision(item.canonical_event_id, f"mkt-{item.canonical_event_id}", when=when)
            for item in fixtures
            if item.market_evaluation_state == "evaluated"
        ],
        discovered_fixtures=fixtures,
        scan_lane=scan_lane,
        operator_summary="lifecycle-test",
        fixture_identity_aliases=aliases,
        fixture_source_events={
            item.canonical_event_id: [
                {
                    "venue": item.source.value,
                    "source_event_id": item.source_event_id,
                    "raw": {"id": item.source_event_id, "status": item.fixture_status},
                }
            ]
            for item in fixtures
        },
    )


def test_explicit_completed_and_final_statuses_drop_immediately() -> None:
    kickoff = NOW - timedelta(minutes=20)
    for status in ("completed", "finished", "final", "settled"):
        fixture = _fixture("done", kickoff=kickoff, fixture_status=status)
        assert classify_scan_lane(fixture, NOW) is ScanLane.DROP
        assert fixture.in_running is None
        assert fixture.fixture_status == status


def test_unknown_t_plus_2h_remains_hot_without_live_or_completed_label() -> None:
    fixture = _fixture("unk", kickoff=NOW - timedelta(hours=2), in_running=None)
    assert classify_scan_lane(fixture, NOW) is ScanLane.HOT
    assert fixture.in_running is None
    assert fixture.fixture_status is None


def test_unknown_beyond_t_plus_3h_leaves_current_radar_without_fabricating_status() -> None:
    fixture = _fixture("late", kickoff=NOW - timedelta(hours=3, minutes=1), in_running=None)
    assert classify_scan_lane(fixture, NOW) is ScanLane.DROP
    assert fixture.in_running is None
    assert fixture.fixture_status is None


def test_postponed_and_delayed_follow_provider_truth_not_kickoff_arithmetic() -> None:
    past = NOW - timedelta(days=5)
    postponed = _fixture("pp", kickoff=past, fixture_status="postponed")
    delayed = _fixture("dl", kickoff=past, fixture_status="delayed")
    rescheduled = _fixture("rs", kickoff=past, fixture_status="rescheduled")
    assert classify_scan_lane(postponed, NOW) is ScanLane.UNIVERSE
    assert classify_scan_lane(delayed, NOW) is ScanLane.UNIVERSE
    assert classify_scan_lane(rescheduled, NOW) is ScanLane.UNIVERSE
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report([postponed, delayed, rescheduled]),
        scan_lane=ScanLane.UNIVERSE,
    )
    ids = {item.canonical_event_id for item in store.inventory(NOW)}
    assert ids == {"pp", "dl", "rs"}
    assert store.detail("pp", now=NOW) is not None
    assert store.hot_identity_scope(NOW) == []


def test_explicit_completed_evicts_current_radar_aliases_and_detail() -> None:
    store = FixtureCurrentStateStore()
    live = _fixture("norwich-wba", kickoff=NOW - timedelta(minutes=40), in_running=True)
    store.upsert_from_report(_report([live]), scan_lane=ScanLane.HOT)
    assert store.resolve_canonical_id("src-norwich-wba") == "norwich-wba"
    assert store.detail("pair-norwich-wba", now=NOW) is not None

    done = _fixture("norwich-wba", kickoff=NOW - timedelta(minutes=40), fixture_status="finished")
    store.upsert_from_report(_report([done]), scan_lane=ScanLane.HOT)
    assert store.inventory(NOW) == []
    assert store.current_radar_rows(NOW) == []
    assert store.hot_identity_scope(NOW) == []
    assert store.resolve_canonical_id("norwich-wba") is None
    assert store.resolve_canonical_id("src-norwich-wba") is None
    assert store.resolve_canonical_id("pair-norwich-wba") is None
    assert store.detail("norwich-wba", now=NOW) is None
    tombstone = store.tombstone_for("norwich-wba")
    assert tombstone is not None
    assert tombstone.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
    assert tombstone.source == "matchbook"
    assert tombstone.provider_status == "finished"
    assert done.fixture_status == "finished"
    assert done.fixture_status_source is VenueName.MATCHBOOK


def test_stale_universe_unknown_cannot_resurrect_terminal_fixture() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(
            [
                _fixture(
                    "west-ham-wolves",
                    kickoff=NOW - timedelta(hours=1),
                    fixture_status="finished",
                )
            ]
        ),
        scan_lane=ScanLane.HOT,
    )
    later = NOW + timedelta(minutes=30)
    stale = _fixture(
        "west-ham-wolves",
        kickoff=NOW - timedelta(hours=1),
        in_running=None,
        fixture_status=None,
        source=VenueName.POLYMARKET,
        matchbook_matched=False,
        polymarket_matched=True,
    )
    store.upsert_from_report(_report([stale], when=later), scan_lane=ScanLane.UNIVERSE)
    assert store.inventory(later) == []
    assert store.detail("west-ham-wolves", now=later) is None
    assert store.hot_identity_scope(later) == []
    assert store.tombstone_for("src-west-ham-wolves") is not None


def test_stale_prior_universe_snapshot_does_not_keep_expired_unknown_fixture() -> None:
    store = FixtureCurrentStateStore()
    historical = _fixture(
        "wrexham-watford",
        kickoff=NOW - timedelta(days=23),
        in_running=None,
        fixture_status=None,
    )
    store.upsert_from_report(
        _report([historical], when=NOW - timedelta(days=23)),
        scan_lane=ScanLane.UNIVERSE,
    )
    assert store.inventory(NOW) == []
    assert store.current_radar_rows(NOW) == []
    assert store.detail("wrexham-watford", now=NOW) is None
    assert store.resolve_canonical_id("src-wrexham-watford") is None
    assert historical.fixture_status is None
    assert historical.in_running is None


def test_three_hour_window_is_not_applied_to_explicit_terminal() -> None:
    fixture = _fixture(
        "millwall-bolton",
        kickoff=NOW - timedelta(hours=1),
        fixture_status="completed",
        in_running=None,
    )
    assert classify_scan_lane(fixture, NOW) is ScanLane.DROP
    assert fixture.fixture_status == "completed"


def test_matchbook_graded_and_closed_are_explicit_terminal() -> None:
    kickoff = NOW - timedelta(minutes=20)
    for status in MATCHBOOK_TERMINAL_EVENT_STATES:
        fixture = _fixture("done", kickoff=kickoff, fixture_status=status)
        assert classify_scan_lane(fixture, NOW) is ScanLane.DROP
        assert should_skip_market_work(fixture, NOW) is True
        assert is_matchbook_lifecycle_status(fixture) is True
        assert terminal_eviction_reason(fixture) == EVICTION_TERMINAL_FROM_MATCHBOOK
        store = FixtureCurrentStateStore()
        store.upsert_from_report(_report([fixture]), scan_lane=ScanLane.HOT)
        assert store.inventory(NOW) == []
        tombstone = store.tombstone_for("done")
        assert tombstone is not None
        assert tombstone.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
        assert tombstone.provider_status == status
        assert tombstone.source == "matchbook"
        assert fixture.in_running is None


def test_matchbook_matched_does_not_make_other_venue_status_matchbook_confirmed() -> None:
    fixture = _fixture(
        "clustered",
        kickoff=NOW - timedelta(minutes=20),
        fixture_status="finished",
        fixture_status_source=VenueName.POLYMARKET,
        source=VenueName.POLYMARKET,
        matchbook_matched=True,
        polymarket_matched=True,
    )
    assert is_matchbook_lifecycle_status(fixture) is False
    assert terminal_eviction_reason(fixture) == EVICTION_TERMINAL
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_report([fixture]), scan_lane=ScanLane.HOT)
    tombstone = store.tombstone_for("clustered")
    assert tombstone is not None
    assert tombstone.reason == EVICTION_TERMINAL
    assert tombstone.source == "polymarket"


@pytest.mark.parametrize("terminal_status", ("finished", "graded", "closed"))
@pytest.mark.parametrize(
    ("later_status", "later_source", "matchbook_matched", "polymarket_matched"),
    [
        (None, VenueName.POLYMARKET, True, True),
        ("postponed", VenueName.POLYMARKET, True, True),
        ("delayed", VenueName.POLYMARKET, True, True),
        ("rescheduled", VenueName.POLYMARKET, True, True),
        (None, VenueName.KALSHI, True, False),
        ("postponed", VenueName.KALSHI, True, False),
        ("delayed", VenueName.KALSHI, True, False),
        ("rescheduled", VenueName.KALSHI, True, False),
    ],
)
def test_matchbook_terminal_not_cleared_by_later_pm_or_kalshi_unknown_or_schedule_exception(
    terminal_status: str,
    later_status: str | None,
    later_source: VenueName,
    matchbook_matched: bool,
    polymarket_matched: bool,
) -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(
            [
                _fixture(
                    "qpr-boro",
                    kickoff=NOW - timedelta(hours=1),
                    fixture_status=terminal_status,
                    fixture_status_source=VenueName.MATCHBOOK,
                )
            ]
        ),
        scan_lane=ScanLane.HOT,
    )
    tombstone = store.tombstone_for("qpr-boro")
    assert tombstone is not None
    assert tombstone.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
    later = NOW + timedelta(minutes=5)
    incoming = _fixture(
        "qpr-boro",
        kickoff=NOW + timedelta(days=2) if later_status else NOW - timedelta(hours=1),
        fixture_status=later_status,
        fixture_status_source=later_source,
        source=later_source,
        matchbook_matched=matchbook_matched,
        polymarket_matched=polymarket_matched,
        kalshi_matched=later_source is VenueName.KALSHI,
    )
    assert is_matchbook_lifecycle_status(incoming) is False
    assert (
        is_trusted_lifecycle_correction(
            incoming,
            observed_at=later,
            tombstone_observed_at=tombstone.observed_at,
            tombstone_reason=tombstone.reason,
            tombstone_source=tombstone.source,
        )
        is False
    )
    store.upsert_from_report(_report([incoming], when=later), scan_lane=ScanLane.UNIVERSE)
    assert store.inventory(later) == []
    assert store.detail("qpr-boro", now=later) is None
    assert store.hot_identity_scope(later) == []
    assert store.resolve_canonical_id("src-qpr-boro") is None
    remaining = store.tombstone_for("qpr-boro")
    assert remaining is not None
    assert remaining.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
    assert remaining.provider_status == terminal_status


@pytest.mark.parametrize(
    ("correction_status", "in_running"),
    [
        ("open", None),
        ("in-play", True),
        ("suspended", False),
        ("rescheduled", None),
    ],
)
def test_later_matchbook_correction_restores_current_radar(
    correction_status: str,
    in_running: bool | None,
) -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(
            [
                _fixture(
                    "qpr-boro",
                    kickoff=NOW - timedelta(hours=1),
                    fixture_status="graded",
                    fixture_status_source=VenueName.MATCHBOOK,
                )
            ]
        ),
        scan_lane=ScanLane.HOT,
    )
    assert store.tombstone_for("qpr-boro") is not None
    later = NOW + timedelta(minutes=5)
    kickoff = NOW + timedelta(days=2) if correction_status == "rescheduled" else NOW - timedelta(hours=1)
    corrected = _fixture(
        "qpr-boro",
        kickoff=kickoff,
        fixture_status=correction_status,
        fixture_status_source=VenueName.MATCHBOOK,
        in_running=in_running,
    )
    store.upsert_from_report(_report([corrected], when=later), scan_lane=ScanLane.UNIVERSE)
    inventory = store.inventory(later)
    assert [item.canonical_event_id for item in inventory] == ["qpr-boro"]
    assert inventory[0].fixture_status == correction_status
    assert inventory[0].fixture_status_source is VenueName.MATCHBOOK
    assert store.tombstone_for("qpr-boro") is None
    assert store.detail("qpr-boro", now=later) is not None


def test_later_matchbook_in_running_without_terminal_status_restores() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(
            [
                _fixture(
                    "qpr-boro",
                    kickoff=NOW - timedelta(hours=1),
                    fixture_status="finished",
                    fixture_status_source=VenueName.MATCHBOOK,
                )
            ]
        ),
        scan_lane=ScanLane.HOT,
    )
    later = NOW + timedelta(minutes=5)
    corrected = _fixture(
        "qpr-boro",
        kickoff=NOW - timedelta(hours=1),
        fixture_status=None,
        fixture_status_source=VenueName.MATCHBOOK,
        in_running=True,
    )
    store.upsert_from_report(_report([corrected], when=later), scan_lane=ScanLane.HOT)
    inventory = store.inventory(later)
    assert [item.canonical_event_id for item in inventory] == ["qpr-boro"]
    assert inventory[0].in_running is True
    assert inventory[0].fixture_status is None
    assert store.tombstone_for("qpr-boro") is None


def test_matchbook_postponed_or_delayed_does_not_clear_matchbook_terminal() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(
            [
                _fixture(
                    "qpr-boro",
                    kickoff=NOW - timedelta(hours=1),
                    fixture_status="finished",
                    fixture_status_source=VenueName.MATCHBOOK,
                )
            ]
        ),
        scan_lane=ScanLane.HOT,
    )
    later = NOW + timedelta(minutes=5)
    for status in ("postponed", "delayed"):
        incoming = _fixture(
            "qpr-boro",
            kickoff=NOW + timedelta(days=2),
            fixture_status=status,
            fixture_status_source=VenueName.MATCHBOOK,
        )
        store.upsert_from_report(_report([incoming], when=later), scan_lane=ScanLane.UNIVERSE)
        assert store.inventory(later) == []
        remaining = store.tombstone_for("qpr-boro")
        assert remaining is not None
        assert remaining.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
        assert remaining.provider_status == "finished"


def test_audit_history_remains_after_current_state_eviction() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: NOW)
    observation = WatchObservation(
        observed_at=NOW,
        canonical_event_id="west-brom-burnley",
        canonical_market_id="mkt-west-brom-burnley",
        competition="Championship",
        home_team="West Bromwich Albion FC",
        away_team="Burnley FC",
        market_family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        legs=[
            WatchLeg(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                currency="GBP",
                native_stake=Decimal("50"),
                gbp_per_unit=Decimal("1"),
                gbp_stake=Decimal("50"),
                net_decimal_odds=Decimal("2.05"),
                cumulative_depth_gbp=Decimal("50"),
            )
        ],
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal("0.004"),
        quote_age_ms=80,
        quote_age_basis="source",
        limiting_depth_gbp=Decimal("50"),
        kickoff_utc=NOW - timedelta(days=22),
    )
    stored = service.observe(observation)
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.reset()
    coordinator._clock = lambda: NOW
    coordinator.record_report(
        _report(
            [
                _fixture(
                    "west-brom-burnley",
                    kickoff=NOW - timedelta(days=22),
                    fixture_status="settled",
                )
            ]
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator.public_status().discovered_fixtures == []
    history = repository.list_observations(stored.opportunity_id)
    assert len(history) == 1
    assert history[0].opportunity_id == stored.opportunity_id
    remaining = [
        item
        for item in repository.list_opportunities()
        if item.canonical_event_id == "west-brom-burnley"
    ]
    assert remaining
    assert remaining[0].canonical_event_id == "west-brom-burnley"
    repository.close()


class LifecycleMatchbook:
    def __init__(self, *, kickoff: datetime, status: str = "open", in_running: bool = True) -> None:
        self.kickoff = kickoff
        self.status = status
        self.in_running = in_running
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {
            "events": [
                {
                    "id": 16401,
                    "name": "Norwich City vs West Bromwich Albion",
                    "start": self.kickoff.isoformat(),
                    "competition-name": "Championship",
                    "status": self.status,
                    "in-running-flag": self.in_running,
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {
            "markets": [
                {
                    "id": 26401,
                    "name": "Match Odds",
                    "runners": [
                        {
                            "id": 1,
                            "name": "Norwich City",
                            "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}],
                        },
                        {
                            "id": 2,
                            "name": "Draw",
                            "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}],
                        },
                        {
                            "id": 3,
                            "name": "West Bromwich Albion",
                            "prices": [{"side": "back", "odds": "3.60", "available-amount": "80"}],
                        },
                    ],
                }
            ]
        }


class LifecyclePolymarket:
    def __init__(self, *, kickoff: datetime) -> None:
        self.kickoff = kickoff
        self.list_markets_calls: list[str] = []
        self.book_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-164",
                "title": "Norwich City vs West Bromwich Albion",
                "startTime": self.kickoff.isoformat(),
                "competition": "Championship",
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return [
            {
                "id": "pm-1x2",
                "question": "Match result?",
                "sportsMarketType": "moneyline",
                "outcomes": '["Norwich City", "Draw", "West Bromwich Albion"]',
                "clobTokenIds": '["h", "d", "a"]',
                "description": "Resolves based on 90 minutes of regulation time.",
                "feesEnabled": False,
            }
        ]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        token = str(outcome_id)
        self.book_calls.append(token)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        return {
            "asset_id": token,
            "timestamp": now_ms - 150,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.42", "size": "100"}],
        }


class CountingPaperScan(PaperScanService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.scan_pair_calls = 0

    def scan_pair(self, *args: Any, **kwargs: Any) -> PaperScanDecision:
        self.scan_pair_calls += 1
        return super().scan_pair(*args, **kwargs)


@pytest.mark.asyncio
async def test_matchbook_finished_stops_hot_market_book_and_economics_calls() -> None:
    kickoff = datetime.now(UTC) - timedelta(minutes=25)
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    matchbook = LifecycleMatchbook(kickoff=kickoff, status="open", in_running=True)
    polymarket = LifecyclePolymarket(kickoff=kickoff)
    paper_scan = CountingPaperScan(intelligence)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=paper_scan,
    )
    clock = {"now": kickoff + timedelta(minutes=25)}
    coordinator = LiveRefreshCoordinator(clock=lambda: clock["now"])
    coordinator.reset()
    coordinator._clock = lambda: clock["now"]
    try:
        live_report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75")),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1")),
            ],
            maximum_execution_risk=100,
            scan_lane=ScanLane.HOT.value,
        )
        coordinator.record_report(live_report, scan_lane=ScanLane.HOT)
        assert matchbook.list_markets_calls
        assert polymarket.list_markets_calls
        assert polymarket.book_calls
        assert paper_scan.scan_pair_calls > 0
        live_markets = list(matchbook.list_markets_calls)
        live_pm_markets = list(polymarket.list_markets_calls)
        live_books = list(polymarket.book_calls)
        live_scans = paper_scan.scan_pair_calls
        assert coordinator.fixture_current_state().hot_identity_scope(clock["now"])

        matchbook.status = "finished"
        matchbook.in_running = False
        finished_report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75")),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1")),
            ],
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
        )
        coordinator.record_report(finished_report, scan_lane=ScanLane.UNIVERSE)
        assert matchbook.list_markets_calls == live_markets
        assert polymarket.list_markets_calls == live_pm_markets
        assert polymarket.book_calls == live_books
        assert paper_scan.scan_pair_calls == live_scans
        store = coordinator.fixture_current_state()
        assert store.hot_identity_scope(clock["now"]) == []
        assert store.inventory(clock["now"]) == []
        finished_id = finished_report.discovered_fixtures[0].canonical_event_id
        assert store.tombstone_for(finished_id) is not None
        assert finished_report.discovered_fixtures[0].fixture_status == "finished"
        assert finished_report.discovered_fixtures[0].fixture_status_source is VenueName.MATCHBOOK
        assert finished_report.discovered_fixtures[0].in_running is False

        clock["now"] = clock["now"] + timedelta(seconds=30)
        plan = coordinator.plan_tick(now=clock["now"])
        hot_report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75")),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1")),
            ],
            maximum_execution_risk=100,
            scan_lane=ScanLane.HOT.value,
            identity_scope=plan.identity_scope or store.hot_identity_scope(clock["now"]),
            known_source_events=store.known_source_events(store.hot_identity_scope(clock["now"])),
        )
        coordinator.record_report(hot_report, scan_lane=ScanLane.HOT)
        assert matchbook.list_markets_calls == live_markets
        assert polymarket.list_markets_calls == live_pm_markets
        assert polymarket.book_calls == live_books
        assert paper_scan.scan_pair_calls == live_scans
        assert coordinator.public_status().discovered_fixtures == []
        assert live_report.discovered_fixtures
        assert live_report.paper_decisions
    finally:
        repository.close()
        coordinator.reset()


@pytest.mark.asyncio
async def test_matchbook_graded_skips_market_book_and_economics_and_is_matchbook_confirmed() -> None:
    kickoff = datetime.now(UTC) - timedelta(minutes=25)
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    matchbook = LifecycleMatchbook(kickoff=kickoff, status="graded", in_running=False)
    polymarket = LifecyclePolymarket(kickoff=kickoff)
    paper_scan = CountingPaperScan(intelligence)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=paper_scan,
    )
    now = kickoff + timedelta(minutes=25)
    coordinator = LiveRefreshCoordinator(clock=lambda: now)
    coordinator.reset()
    coordinator._clock = lambda: now
    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75")),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1")),
            ],
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
        )
        coordinator.record_report(report, scan_lane=ScanLane.UNIVERSE)
        assert matchbook.list_markets_calls == []
        assert polymarket.list_markets_calls == []
        assert polymarket.book_calls == []
        assert paper_scan.scan_pair_calls == 0
        assert report.discovered_fixtures
        fixture = report.discovered_fixtures[0]
        assert fixture.fixture_status == "graded"
        assert fixture.fixture_status_source is VenueName.MATCHBOOK
        store = coordinator.fixture_current_state()
        assert store.inventory(now) == []
        tombstone = store.tombstone_for(fixture.canonical_event_id)
        assert tombstone is not None
        assert tombstone.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
        assert tombstone.source == "matchbook"
        assert tombstone.provider_status == "graded"
    finally:
        repository.close()
        coordinator.reset()
