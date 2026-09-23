from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.historical import get_facts_repository, get_odds_repository
from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.arbitrage.watchlist.economics import distance_to_trigger_pp
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.historical.adapters import SyntheticHistoricalAdapter
from sports_hedge.historical.catalog import PREMIER_LEAGUE
from sports_hedge.historical.ingestion import HistoricalIngestionService
from sports_hedge.historical.repository import SqliteHistoricalRepository
from sports_hedge.odds.adapters.synthetic import SyntheticOddsAdapter
from sports_hedge.odds.ingestion import OddsIngestionService
from sports_hedge.odds.mapping import map_raw_record
from sports_hedge.odds.models import QuoteType, RawOddsRecord, VenueKind
from sports_hedge.odds.repository import SqliteOddsRepository

OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
KICKOFF = datetime(2025, 8, 16, 15, 0, tzinfo=UTC)


def _leg() -> WatchLeg:
    return WatchLeg(
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


def test_tracked_board_keeps_negative_net_margin_and_does_not_reclassify_rejected() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)

    below_even = WatchObservation(
        observed_at=OBSERVED,
        canonical_event_id="evt-neg",
        canonical_market_id="mkt-neg",
        competition="Premier League",
        home_team="Arsenal",
        away_team="Fulham",
        market_family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        legs=[_leg()],
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal("-0.024"),
        gross_edge=Decimal("0.0001"),
        implied_probability_sum=Decimal("1") / Decimal("0.976"),
        quote_age_ms=120,
        quote_age_basis="retrieval",
        limiting_depth_gbp=Decimal("50"),
    )
    rejected = WatchObservation(
        observed_at=OBSERVED,
        canonical_event_id="evt-rej",
        canonical_market_id="mkt-rej",
        competition="Premier League",
        home_team="Brighton",
        away_team="Leeds",
        market_family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        legs=[_leg()],
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal("0.02"),
        implied_probability_sum=Decimal("1") / Decimal("1.02"),
        rejection_reasons=["missing_fx_rate:USD"],
        quote_age_ms=80,
    )
    service.observe(below_even)
    service.observe(rejected)
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    from test_tracked_current_snapshot import _report

    coordinator.record_report(_report("mkt-neg", "mkt-rej"))

    try:
        tracked = client.get("/paper/watchlist/tracked")
        assert tracked.status_code == 200
        body = tracked.json()
        ids = {row["canonical_market_id"]: row for row in body}
        assert "mkt-neg" in ids
        assert float(ids["mkt-neg"]["current_net_edge"]) < 0
        assert float(ids["mkt-neg"]["distance_to_trigger_pp"]) == float(
            distance_to_trigger_pp(Decimal("-0.024"), Decimal("0.01"))
        )
        assert ids["mkt-neg"]["status"] == "WATCHING"
        assert ids["mkt-neg"]["is_arbitrage"] is False
        assert ids["mkt-neg"]["guaranteed_profit_gbp"] is None
        assert "mkt-rej" in ids
        assert ids["mkt-rej"]["status"] == "REJECTED"
        assert [row["canonical_market_id"] for row in body] == ["mkt-neg", "mkt-rej"]

        near = client.get("/paper/watchlist/near").json()
        assert [row["canonical_market_id"] for row in near] == ["mkt-neg"]
        assert near[0]["quote_age_basis"] == "retrieval"
        triggered = client.get("/paper/watchlist/triggered").json()
        assert triggered == []
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def _ah_record(*, source_id: str, line: Decimal, quote_type: QuoteType) -> RawOddsRecord:
    return RawOddsRecord(
        source="football-data.co.uk",
        source_market_id=source_id,
        source_reference=source_id,
        competition="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=KICKOFF,
        market_family=MarketFamily.ASIAN_HANDICAP,
        period=FootballPeriod.FULL_TIME,
        line=line,
        selection="home",
        decimal_odds=Decimal("1.90"),
        quote_type=quote_type,
        venue_kind=VenueKind.BOOKMAKER,
        retrieved_at=KICKOFF,
        observed_at=KICKOFF,
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            line=line,
            extra_time_included=False,
            penalties_included=False,
        ),
        semantics_complete=True,
    )


def test_historical_coverage_is_repository_derived_and_separates_ah_line_shifts() -> None:
    facts = SqliteHistoricalRepository()
    HistoricalIngestionService(facts).ingest_adapter(
        SyntheticHistoricalAdapter(),
        competition_id=PREMIER_LEAGUE.competition_id,
        season_label="2025/26",
    )
    odds = SqliteOddsRepository()
    OddsIngestionService(odds).ingest_adapter(SyntheticOddsAdapter())
    odds.insert_observation(map_raw_record(_ah_record(
        source_id="ah-open",
        line=Decimal("-0.5"),
        quote_type=QuoteType.OPENING,
    )))
    odds.insert_observation(map_raw_record(_ah_record(
        source_id="ah-close",
        line=Decimal("-0.25"),
        quote_type=QuoteType.CLOSING,
    )))

    app.dependency_overrides[get_facts_repository] = lambda: facts
    app.dependency_overrides[get_odds_repository] = lambda: odds
    client = TestClient(app)
    try:
        response = client.get("/research/historical/coverage")
        assert response.status_code == 200
        payload = response.json()
        assert payload["data_class"] == "REAL_HISTORICAL"
        assert payload["match_count"] == facts.count_matches()
        assert payload["stored_observation_count"] == odds.count_observations()
        assert payload["same_line_opening_closing_pairs"] == odds.count_same_line_opening_closing_pairs()
        assert payload["asian_handicap_line_shifts"] >= 1
        assert payload["analogue_model"] == "UNAVAILABLE"
        assert "never causation" in payload["analogue_note"]
        assert "structural line shifts" in payload["movement_semantics"]
        assert payload["match_count"] != 4660
    finally:
        app.dependency_overrides.clear()
        facts.close()
        odds.close()
