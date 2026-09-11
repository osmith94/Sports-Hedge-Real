from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.application.market_observation import MatchbookObservationBuilder
from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.trends import TrendExplorer, TrendMetric, TrendQuery


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def test_observation_snapshot_retains_period_line_and_settlement_semantics() -> None:
    observation = MatchbookObservationBuilder().build(
        {
            "id": 100,
            "name": "Newcastle United vs Chelsea",
            "start": KICKOFF.isoformat(),
            "competition-name": "Premier League",
        },
        {
            "id": 200,
            "name": "Total Goals 2.5",
            "runners": [
                {
                    "id": 1,
                    "name": "Over 2.5",
                    "prices": [{"side": "back", "odds": "1.95", "available-amount": "100"}],
                },
                {
                    "id": 2,
                    "name": "Under 2.5",
                    "prices": [{"side": "back", "odds": "2.02", "available-amount": "100"}],
                },
            ],
        },
        observed_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )

    snapshots = observation.snapshots(
        canonical_event_id="evt:test",
        canonical_market_id="mkt:test",
    )
    assert len(snapshots) == 2
    for snapshot in snapshots:
        assert snapshot.market_family == MarketFamily.TOTAL_GOALS
        assert snapshot.period == FootballPeriod.FULL_TIME
        assert snapshot.market_line == Decimal("2.5")
        assert snapshot.settlement_scope == SettlementScope.REGULATION_TIME
        assert snapshot.settlement_key == observation.market.settlement.deterministic_key()


def test_repository_migrates_legacy_snapshot_table_without_losing_existing_shape(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE market_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            observed_at TEXT NOT NULL,
            venue TEXT NOT NULL,
            canonical_event_id TEXT NOT NULL,
            canonical_market_id TEXT NOT NULL,
            canonical_outcome TEXT NOT NULL,
            market_family TEXT NOT NULL,
            competition TEXT,
            home_team TEXT,
            away_team TEXT,
            source_event_id TEXT,
            source_market_id TEXT,
            source_outcome_id TEXT,
            kickoff_utc TEXT,
            decimal_odds TEXT NOT NULL,
            implied_probability TEXT NOT NULL,
            best_back_odds TEXT,
            best_lay_odds TEXT,
            back_size TEXT,
            lay_size TEXT,
            spread_decimal TEXT,
            total_liquidity TEXT,
            source_latency_ms INTEGER,
            order_book_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    repository = SqliteMarketIntelligenceRepository(database)
    try:
        snapshot = MarketSnapshot(
            observed_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
            venue=VenueName.MATCHBOOK,
            canonical_event_id="evt:legacy",
            canonical_market_id="mkt:legacy",
            canonical_outcome="over",
            market_family=MarketFamily.TOTAL_GOALS,
            period=FootballPeriod.FIRST_HALF,
            market_line=Decimal("1.5"),
            settlement_scope=SettlementScope.PERIOD_ONLY,
            settlement_key="period-rule",
            decimal_odds=Decimal("1.9"),
        )
        repository.append_snapshot(snapshot)
        restored = repository.list_snapshots(canonical_market_id="mkt:legacy")
        assert len(restored) == 1
        assert restored[0].period == FootballPeriod.FIRST_HALF
        assert restored[0].market_line == Decimal("1.5")
        assert restored[0].settlement_scope == SettlementScope.PERIOD_ONLY
        assert restored[0].settlement_key == "period-rule"
    finally:
        repository.close()

    inspect = sqlite3.connect(database)
    columns = {row[1] for row in inspect.execute("PRAGMA table_info(market_snapshots)").fetchall()}
    inspect.close()
    assert {"period", "market_line", "settlement_scope", "settlement_key"}.issubset(columns)


def _snapshot(event: int, minute: int, line: str, probability: str) -> MarketSnapshot:
    implied = Decimal(probability)
    return MarketSnapshot(
        observed_at=datetime(2026, 9, 20 + event, 12, minute, tzinfo=UTC),
        venue=VenueName.MATCHBOOK,
        canonical_event_id=f"evt:{event}",
        canonical_market_id=f"mkt:{event}:{line}",
        canonical_outcome="over",
        market_family=MarketFamily.TOTAL_GOALS,
        period=FootballPeriod.FULL_TIME,
        market_line=Decimal(line),
        settlement_scope=SettlementScope.REGULATION_TIME,
        settlement_key=f"regulation|{line}",
        decimal_odds=Decimal("1") / implied,
        implied_probability=implied,
    )


def test_trend_explorer_can_isolate_specific_market_line_and_settlement() -> None:
    snapshots = [
        _snapshot(1, 0, "2.5", "0.50"),
        _snapshot(1, 5, "2.5", "0.55"),
        _snapshot(2, 0, "3.5", "0.40"),
        _snapshot(2, 5, "3.5", "0.42"),
    ]
    explorer = TrendExplorer(minimum_sample_size=1)
    summary = explorer.analyze(
        TrendMetric.OPEN_TO_CLOSE_PROBABILITY,
        snapshots,
        query=TrendQuery(
            market_family=MarketFamily.TOTAL_GOALS,
            period=FootballPeriod.FULL_TIME,
            market_line=Decimal("2.5"),
            settlement_scope=SettlementScope.REGULATION_TIME,
        ),
    )

    assert summary.sample_size == 1
    assert summary.observations[0].market_line == Decimal("2.5")
    assert summary.observations[0].period == FootballPeriod.FULL_TIME
    assert summary.observations[0].settlement_scope == SettlementScope.REGULATION_TIME
    assert round(summary.observations[0].value, 6) == 0.05
