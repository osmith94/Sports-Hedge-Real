"""One post-kickoff operator lifecycle for every supported fixture sport.

Data class: deterministic current-state fixtures plus one NFL normalisation
path. Not live venue data. Provider ``in_running`` is not fabricated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_issue200_universe_hot_promotion import _facts

from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    InventoryPairResult,
)
from sports_hedge.application.fixture_state import matchbook_fixture_state
from sports_hedge.application.scan_lanes import (
    HOT_REASON_IN_PLAY,
    HOT_REASON_POST_KICKOFF_STATUS_PENDING,
    ScanLane,
    hot_reason_labels,
    kickoff_horizon_reason_label,
)
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.nfl.constants import NFL_SPORT
from sports_hedge.normalization.venues import MatchbookNormalizer

NOW = datetime(2026, 10, 4, 18, 0, tzinfo=UTC)
KICKOFF = NOW - timedelta(minutes=40)

SPORTS = [
    ("football", "Premier League", "Arsenal", "Chelsea"),
    ("american_football", "NFL", "Chicago Bears", "New York Jets"),
    ("basketball", "NBA", "Boston Celtics", "New York Knicks"),
    ("baseball", "MLB", "Chicago Cubs", "St. Louis Cardinals"),
    ("basketball", "NCAAB", "Duke", "North Carolina"),
    ("tennis", "ATP", "Jannik Sinner", "Carlos Alcaraz"),
]


def _fixture(
    sport: str,
    competition: str,
    home: str,
    away: str,
    *,
    when: datetime = NOW,
    kickoff: datetime = KICKOFF,
    in_running: bool | None = None,
    fixture_status: str | None = "open",
    evaluation: str = "evaluated",
    reason: str | None = None,
    equivalents: int | None = 4,
) -> DiscoveredFixture:
    event_id = f"evt/{sport}/{competition}/{home}"
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=f"src-{event_id}",
        canonical_event_id=event_id,
        home_team=home,
        away_team=away,
        competition=competition,
        sport=sport,
        kickoff_utc=kickoff,
        last_seen_at=when,
        in_running=in_running,
        fixture_status=fixture_status,
        fixture_status_source=VenueName.MATCHBOOK if fixture_status else None,
        matchbook_matched=True,
        polymarket_matched=True,
        market_evaluation_state=evaluation,
        market_evaluation_reason=reason,
        matched_equivalent_count=equivalents,
        opportunity_state="matched" if evaluation == "evaluated" else "not_evaluated",
    )


def _row(event_id: str) -> FixtureMarketInventoryRow:
    return FixtureMarketInventoryRow(
        display_name="Game Winner",
        family="game_winner",
        period="full_time",
        comparison_status=InventoryComparisonStatus.MATCHED_EQUIVALENT,
        matchbook=_facts(VenueName.MATCHBOOK, source_market_id=f"mb-{event_id}"),
        polymarket=_facts(VenueName.POLYMARKET, source_market_id=f"pm-{event_id}"),
        pair_results=[
            InventoryPairResult(
                left_venue=VenueName.MATCHBOOK,
                right_venue=VenueName.POLYMARKET,
                entered_solver=False,
                solver_is_arbitrage=False,
            )
        ],
    )


def _report(fixture: DiscoveredFixture, *, when: datetime, markets: bool) -> CollectionReport:
    rows = [_row(fixture.canonical_event_id)] if markets else []
    return CollectionReport(
        started_at=when,
        completed_at=when,
        discovered_fixtures=[fixture],
        fixture_markets={fixture.canonical_event_id: rows},
        scan_lane=ScanLane.HOT.value,
        fixture_identity_aliases={
            fixture.canonical_event_id: fixture.canonical_event_id,
            fixture.source_event_id: fixture.canonical_event_id,
        },
    )


def _shown(
    store: FixtureCurrentStateStore, fixture: DiscoveredFixture, when: datetime
) -> DiscoveredFixture:
    rows = {item.canonical_event_id: item for item in store.inventory(when)}
    return rows[fixture.canonical_event_id]


@pytest.mark.parametrize(("sport", "competition", "home", "away"), SPORTS)
def test_before_kickoff_is_not_in_play(sport: str, competition: str, home: str, away: str) -> None:
    store = FixtureCurrentStateStore()
    fixture = _fixture(
        sport,
        competition,
        home,
        away,
        kickoff=NOW + timedelta(minutes=40),
        equivalents=4,
        in_running=False,
    )
    store.upsert_from_report(
        _report(fixture, when=NOW, markets=True), scan_lane=ScanLane.HOT, now=NOW
    )
    row = _shown(store, fixture, NOW)
    assert HOT_REASON_IN_PLAY not in (row.hot_reasons or [])
    assert row.hot_reasons == [kickoff_horizon_reason_label()]
    assert row.in_running is False


@pytest.mark.parametrize(("sport", "competition", "home", "away"), SPORTS)
def test_after_kickoff_with_equivalents_is_in_play(
    sport: str, competition: str, home: str, away: str
) -> None:
    store = FixtureCurrentStateStore()
    fixture = _fixture(sport, competition, home, away, equivalents=4, in_running=None)
    store.upsert_from_report(
        _report(fixture, when=NOW, markets=True), scan_lane=ScanLane.HOT, now=NOW
    )
    row = _shown(store, fixture, NOW)
    assert row.hot_reasons == [HOT_REASON_IN_PLAY]
    assert (row.matched_equivalent_count or 0) > 0
    assert row.in_running is None
    assert row.market_evaluation_state == "evaluated"


@pytest.mark.parametrize(("sport", "competition", "home", "away"), SPORTS)
def test_provider_in_running_is_not_rewritten(
    sport: str, competition: str, home: str, away: str
) -> None:
    live = _fixture(
        sport, competition, home, away, in_running=True, equivalents=0, fixture_status="in-play"
    )
    assert hot_reason_labels(
        live,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.HOT,
        qualifying_promotion=False,
    ) == [HOT_REASON_IN_PLAY]
    assert live.in_running is True


@pytest.mark.parametrize(("sport", "competition", "home", "away"), SPORTS)
def test_explicit_terminal_leaves_immediately(
    sport: str, competition: str, home: str, away: str
) -> None:
    store = FixtureCurrentStateStore()
    done = _fixture(
        sport,
        competition,
        home,
        away,
        fixture_status="completed",
        equivalents=4,
        in_running=None,
    )
    store.upsert_from_report(_report(done, when=NOW, markets=True), scan_lane=ScanLane.HOT, now=NOW)
    assert store.inventory(NOW) == []
    assert done.in_running is None
    assert done.fixture_status == "completed"


@pytest.mark.parametrize(("sport", "competition", "home", "away"), SPORTS)
def test_successful_zero_equivalents_leave_current_radar(
    sport: str, competition: str, home: str, away: str
) -> None:
    store = FixtureCurrentStateStore()
    active = _fixture(sport, competition, home, away, equivalents=3, in_running=None)
    store.upsert_from_report(
        _report(active, when=NOW, markets=True), scan_lane=ScanLane.HOT, now=NOW
    )
    later = NOW + timedelta(seconds=20)
    closed = _fixture(
        sport,
        competition,
        home,
        away,
        when=later,
        equivalents=0,
        in_running=None,
        fixture_status="open",
    )
    store.upsert_from_report(
        _report(closed, when=later, markets=False), scan_lane=ScanLane.HOT, now=later
    )
    assert store.inventory(later) == []
    assert closed.in_running is None
    assert closed.fixture_status == "open"


@pytest.mark.parametrize(("sport", "competition", "home", "away"), SPORTS)
def test_incomplete_refresh_does_not_end_fixture(
    sport: str, competition: str, home: str, away: str
) -> None:
    store = FixtureCurrentStateStore()
    active = _fixture(sport, competition, home, away, equivalents=4, in_running=False)
    store.upsert_from_report(
        _report(active, when=NOW, markets=True), scan_lane=ScanLane.HOT, now=NOW
    )
    later = NOW + timedelta(seconds=15)
    failed = _fixture(
        sport,
        competition,
        home,
        away,
        when=later,
        evaluation="evaluated",
        reason="provider_timeout",
        equivalents=0,
        in_running=False,
    )
    store.upsert_from_report(
        _report(failed, when=later, markets=False), scan_lane=ScanLane.HOT, now=later
    )
    row = _shown(store, active, later)
    assert (row.matched_equivalent_count or 0) > 0
    assert HOT_REASON_IN_PLAY in (row.hot_reasons or [])
    assert HOT_REASON_POST_KICKOFF_STATUS_PENDING not in (row.hot_reasons or [])
    assert row.in_running is False
    assert store.tombstone_for(active.canonical_event_id) is None


def test_nfl_normalised_post_kickoff_keeps_proved_equivalent_through_nonproving_refresh() -> None:
    """NFL uses the same operator rule after the real Matchbook normaliser.

    Football often still reads IN PLAY from provider ``in_running``. NFL
    Matchbook payloads frequently omit that flag, so the label depends on a
    current equivalent count. A price-engine shaped refresh that does not
    prove zero, including a non-comparable row, must not revoke that count.
    A later relationship TTL expiry still displays zero.
    """

    raw_event = {
        "id": "mb-bears-jets",
        "name": "New York Jets at Chicago Bears",
        "start": KICKOFF.isoformat(),
        "sport-id": "1",
        "sport-name": "American Football",
        "meta-tags": [
            {"id": "1", "name": "American Football", "type": "SPORT"},
            {"id": "491503123380010", "name": "NFL", "type": "COMPETITION"},
        ],
        "status": "open",
    }
    provider_state = matchbook_fixture_state(raw_event)
    assert provider_state.in_running is None
    event = MatchbookNormalizer().normalize_event(raw_event)
    market = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": "mb-nfl-ml",
            "name": "Money Line",
            "market-type": "money_line",
            "status": "open",
            "runners": [
                {"id": "away", "name": "New York Jets"},
                {"id": "home", "name": "Chicago Bears"},
            ],
        },
    )
    assert event.sport == NFL_SPORT
    assert market.family is MarketFamily.GAME_WINNER
    family = market.family.value
    fixture = DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=event.source_event_id,
        canonical_event_id="evt/nfl/bears-jets",
        home_team=event.home_team,
        away_team=event.away_team,
        competition=event.competition,
        sport=event.sport,
        kickoff_utc=event.kickoff_utc,
        last_seen_at=NOW,
        in_running=provider_state.in_running,
        fixture_status=provider_state.venue_status,
        fixture_status_source=VenueName.MATCHBOOK,
        matchbook_matched=True,
        polymarket_matched=True,
        market_evaluation_state="evaluated",
        matched_equivalent_count=2,
        opportunity_state="matched",
    )
    row = FixtureMarketInventoryRow(
        display_name="Money Line",
        family=family,
        period=market.period.value,
        comparison_status=InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
        matchbook=_facts(VenueName.MATCHBOOK, source_market_id=market.source_market_id),
        polymarket=_facts(VenueName.POLYMARKET, source_market_id="pm-nfl-ml"),
        pair_results=[
            InventoryPairResult(
                left_venue=VenueName.MATCHBOOK,
                right_venue=VenueName.POLYMARKET,
                entered_solver=False,
                solver_is_arbitrage=False,
            )
        ],
    )
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        CollectionReport(
            started_at=NOW,
            completed_at=NOW,
            discovered_fixtures=[fixture],
            fixture_markets={fixture.canonical_event_id: [row]},
            scan_lane=ScanLane.HOT.value,
            fixture_identity_aliases={
                fixture.canonical_event_id: fixture.canonical_event_id,
                fixture.source_event_id: fixture.canonical_event_id,
            },
        ),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    shown = _shown(store, fixture, NOW)
    assert shown.sport == NFL_SPORT
    assert shown.hot_reasons == [HOT_REASON_IN_PLAY]
    assert shown.in_running is None

    later = NOW + timedelta(seconds=30)
    refresh = fixture.model_copy(
        update={
            "last_seen_at": later,
            "matched_equivalent_count": None,
            "market_evaluation_state": "evaluated",
            "market_evaluation_reason": None,
        }
    )
    downgraded = row.model_copy(
        update={
            "comparison_status": InventoryComparisonStatus.OTHER,
            "reason": "not_equivalent",
            "rejection_reasons": ["not_equivalent"],
        }
    )
    store.upsert_from_report(
        CollectionReport(
            started_at=later,
            completed_at=later,
            discovered_fixtures=[refresh],
            fixture_markets={fixture.canonical_event_id: [downgraded]},
            scan_lane=ScanLane.HOT.value,
            scan_diagnostics={"price_engine": True},
            fixture_identity_aliases={
                fixture.canonical_event_id: fixture.canonical_event_id,
                fixture.source_event_id: fixture.canonical_event_id,
            },
        ),
        scan_lane=ScanLane.HOT,
        now=later,
        pricing_refresh=True,
    )
    kept = _shown(store, fixture, later)
    assert kept.hot_reasons == [HOT_REASON_IN_PLAY]
    assert (kept.matched_equivalent_count or 0) > 0
    assert kept.in_running is None
    assert HOT_REASON_POST_KICKOFF_STATUS_PENDING not in (kept.hot_reasons or [])
    assert store.tombstone_for(fixture.canonical_event_id) is None

    expired_at = later + timedelta(seconds=2)
    aged = store.inventory(expired_at, hot_ttl_seconds=1, universe_ttl_seconds=1)
    assert store.tombstone_for(fixture.canonical_event_id) is None
    if aged:
        assert aged[0].matched_equivalent_count == 0
        assert HOT_REASON_IN_PLAY not in (aged[0].hot_reasons or [])
        assert aged[0].in_running is None
