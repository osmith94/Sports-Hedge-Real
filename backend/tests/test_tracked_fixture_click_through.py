from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.fixture_clusters import (
    VenueEvent,
    cluster_canonical_event_id,
    cluster_identity_aliases,
    cluster_venue_events,
)
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import CanonicalEvent, FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.normalization.identity import canonical_matched_event_id
from sports_hedge.paper.models import PaperScanDecision

OBSERVED = datetime.now(UTC)
KICKOFF = OBSERVED + timedelta(days=3)


def _venue_event(venue: VenueName, source_event_id: str) -> VenueEvent:
    canonical = CanonicalEvent(
        competition="Premier League",
        home_team="Leeds United",
        away_team="Leicester City",
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_event_id,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": "Leeds United vs Leicester City"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def _three_venue_cluster():
    clusters, _counts = cluster_venue_events(
        matchbook=[_venue_event(VenueName.MATCHBOOK, "mb-leeds")],
        polymarket=[_venue_event(VenueName.POLYMARKET, "pm-leeds")],
        kalshi=[_venue_event(VenueName.KALSHI, "k-leeds")],
        matcher=EventMatcher(),
        max_event_pairs=25,
    )
    assert len(clusters) == 1
    return clusters[0]


def _fixture(cluster, *, canonical_event_id: str) -> DiscoveredFixture:
    canonical = cluster.anchor.canonical
    return DiscoveredFixture(
        source=cluster.anchor.venue,
        source_event_id=cluster.anchor.source_event_id,
        canonical_event_id=canonical_event_id,
        home_team=canonical.home_team,
        away_team=canonical.away_team,
        competition=canonical.competition,
        kickoff_utc=canonical.kickoff_utc,
        matchbook_matched=True,
        polymarket_matched=True,
        kalshi_matched=True,
        last_seen_at=OBSERVED,
        matched_equivalent_count=1,
        opportunity_state="matched",
        market_evaluation_state="evaluated",
    )


def _decision(
    *,
    event_id: str,
    market_id: str,
    fixture_canonical_event_id: str | None,
) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_event_id=event_id,
        canonical_market_id=market_id,
        fixture_canonical_event_id=fixture_canonical_event_id,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
    )


def _observation(
    *,
    event_id: str,
    market_id: str,
    observed_at: datetime = OBSERVED,
) -> WatchObservation:
    return WatchObservation(
        observed_at=observed_at,
        canonical_event_id=event_id,
        canonical_market_id=market_id,
        settlement_key="regulation_time|full_time",
        competition="Premier League",
        home_team="Leeds United",
        away_team="Leicester City",
        market_family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        legs=[
            WatchLeg(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb-1x2",
                currency="GBP",
                native_stake=Decimal("50"),
                gbp_per_unit=Decimal("1"),
                gbp_stake=Decimal("50"),
                net_decimal_odds=Decimal("2.05"),
                cumulative_depth_gbp=Decimal("50"),
            )
        ],
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal("0.008"),
        quote_age_ms=80,
        quote_age_basis="source",
        limiting_depth_gbp=Decimal("50"),
    )


def _report(
    *,
    fixtures: list[DiscoveredFixture],
    decisions: list[PaperScanDecision],
    aliases: dict[str, str] | None = None,
    fixture_markets: dict | None = None,
    when: datetime = OBSERVED,
) -> CollectionReport:
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=decisions,
        discovered_fixtures=fixtures,
        fixture_markets=fixture_markets or {item.canonical_event_id: [] for item in fixtures},
        fixture_identity_aliases=aliases or {},
        operator_summary="current collection",
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
    )


def test_three_venue_pair_id_is_not_the_cluster_fixture_id() -> None:
    cluster = _three_venue_cluster()
    cluster_id = cluster_canonical_event_id(cluster)
    pair_id = canonical_matched_event_id(
        [cluster.matchbook.canonical, cluster.polymarket.canonical]
    )
    assert pair_id != cluster_id
    assert cluster_identity_aliases(cluster)[pair_id] == cluster_id


def test_tracked_row_id_resolves_to_cluster_fixture_not_a_name_guess() -> None:
    cluster = _three_venue_cluster()
    cluster_id = cluster_canonical_event_id(cluster)
    pair_id = canonical_matched_event_id(
        [cluster.matchbook.canonical, cluster.polymarket.canonical]
    )
    aliases = cluster_identity_aliases(cluster)
    fixture = _fixture(cluster, canonical_event_id=cluster_id)
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    service.observe(_observation(event_id=pair_id, market_id="mkt-mb-pm"))
    coordinator.record_report(
        _report(
            fixtures=[fixture],
            decisions=[
                _decision(
                    event_id=pair_id,
                    market_id="mkt-mb-pm",
                    fixture_canonical_event_id=cluster_id,
                )
            ],
            aliases=aliases,
        )
    )
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        tracked = client.get("/paper/watchlist/tracked")
        assert tracked.status_code == 200
        rows = tracked.json()
        assert len(rows) == 1
        tracked_id = rows[0]["canonical_event_id"]
        assert tracked_id == pair_id
        detail = client.get(f"/operations/fixtures/{tracked_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["fixture"]["canonical_event_id"] == cluster_id
        assert body["fixture"]["home_team"] == "Leeds United"
        assert body["execution_enabled"] is False
        assert body["paper_mode"] == "paper"
        by_cluster = client.get(f"/operations/fixtures/{cluster_id}")
        assert by_cluster.status_code == 200
        assert by_cluster.json()["fixture"]["canonical_event_id"] == cluster_id
        named = client.get("/operations/fixtures/Leeds%20United%20v%20Leicester%20City")
        assert named.status_code == 404
        assert "demo" not in named.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_collector_stamped_cluster_id_on_tracked_also_resolves() -> None:
    cluster = _three_venue_cluster()
    cluster_id = cluster_canonical_event_id(cluster)
    pair_id = canonical_matched_event_id(
        [cluster.matchbook.canonical, cluster.polymarket.canonical]
    )
    fixture = _fixture(cluster, canonical_event_id=cluster_id)
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    service.observe(_observation(event_id=cluster_id, market_id="mkt-stamped"))
    coordinator.record_report(
        _report(
            fixtures=[fixture],
            decisions=[
                _decision(
                    event_id=cluster_id,
                    market_id="mkt-stamped",
                    fixture_canonical_event_id=cluster_id,
                )
            ],
            aliases=cluster_identity_aliases(cluster),
        )
    )
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        tracked_id = client.get("/paper/watchlist/tracked").json()[0]["canonical_event_id"]
        assert tracked_id == cluster_id
        assert client.get(f"/operations/fixtures/{tracked_id}").status_code == 200
        assert client.get(f"/operations/fixtures/{pair_id}").status_code == 200
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_refresh_keeps_in_ttl_identities_across_later_upserts() -> None:
    cluster = _three_venue_cluster()
    cluster_id = cluster_canonical_event_id(cluster)
    pair_id = canonical_matched_event_id(
        [cluster.matchbook.canonical, cluster.polymarket.canonical]
    )
    other = DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id="mb-other",
        canonical_event_id="evt:other-current",
        home_team="Arsenal",
        away_team="Fulham",
        competition="Premier League",
        kickoff_utc=KICKOFF,
        last_seen_at=OBSERVED,
        market_evaluation_state="evaluated",
    )
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    clock = {"now": OBSERVED}
    service = WatchlistService(repository, clock=lambda: clock["now"])
    service.observe(_observation(event_id=pair_id, market_id="mkt-leeds"))
    coordinator.record_report(
        _report(
            fixtures=[_fixture(cluster, canonical_event_id=cluster_id)],
            decisions=[
                _decision(
                    event_id=pair_id,
                    market_id="mkt-leeds",
                    fixture_canonical_event_id=cluster_id,
                )
            ],
            aliases=cluster_identity_aliases(cluster),
        )
    )
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        assert client.get(f"/operations/fixtures/{pair_id}").status_code == 200
        clock["now"] = OBSERVED + timedelta(seconds=30)
        service.observe(
            _observation(
                event_id="evt:other-current",
                market_id="mkt-other",
                observed_at=clock["now"],
            )
        )
        coordinator.record_report(
            _report(
                fixtures=[other],
                decisions=[
                    _decision(
                        event_id="evt:other-current",
                        market_id="mkt-other",
                        fixture_canonical_event_id="evt:other-current",
                    )
                ],
                aliases={"evt:other-current": "evt:other-current", "mb-other": "evt:other-current"},
            )
        )
        tracked = client.get("/paper/watchlist/tracked").json()
        assert {row["canonical_market_id"] for row in tracked} == {"mkt-leeds", "mkt-other"}
        assert client.get(f"/operations/fixtures/{pair_id}").status_code == 200
        assert client.get("/operations/fixtures/evt:other-current").status_code == 200
        activity = client.get("/paper/watchlist/activity").json()
        assert any(event["opportunity_id"] == "watch:mkt-leeds" for event in activity)
        assert repository.get("watch:mkt-leeds") is not None
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_same_fixture_stays_resolvable_across_cohort_when_still_current() -> None:
    cluster = _three_venue_cluster()
    cluster_id = cluster_canonical_event_id(cluster)
    pair_id = canonical_matched_event_id(
        [cluster.matchbook.canonical, cluster.polymarket.canonical]
    )
    fixture = _fixture(cluster, canonical_event_id=cluster_id)
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator.record_report(
        _report(
            fixtures=[fixture],
            decisions=[
                _decision(
                    event_id=pair_id,
                    market_id="mkt-leeds",
                    fixture_canonical_event_id=cluster_id,
                )
            ],
            aliases=cluster_identity_aliases(cluster),
        )
    )
    leftover = fixture.model_copy(
        update={
            "market_evaluation_state": "not_evaluated_scan_deadline",
            "opportunity_state": "not_evaluated",
            "matched_equivalent_count": None,
        }
    )
    coordinator.record_report(
        _report(
            fixtures=[leftover],
            decisions=[],
            aliases={cluster_id: cluster_id, leftover.source_event_id: cluster_id},
        )
    )
    client = TestClient(app)
    try:
        tracked = client.get("/paper/watchlist/tracked").json()
        assert tracked == []
        still_current = client.get(f"/operations/fixtures/{pair_id}")
        assert still_current.status_code == 200
        assert still_current.json()["fixture"]["canonical_event_id"] == cluster_id
    finally:
        coordinator.reset()


def test_unknown_fixture_id_is_honest_404_without_demo_substitution() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    client = TestClient(app)
    try:
        missing = client.get("/operations/fixtures/evt:unknown-expired")
        assert missing.status_code == 404
        detail = missing.json()["detail"]
        assert "No collected fixture" in detail
        assert "demo" not in detail.lower()
        assert "arsenal" not in detail.lower()
        health = client.get("/health").json()
        assert health["execution_enabled"] is False
    finally:
        coordinator.reset()
