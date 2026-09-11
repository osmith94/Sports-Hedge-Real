from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.analytics import MarketIntelligenceAnalytics
from sports_hedge.market_intelligence.models import (
    AnnotationCategory,
    KickoffBucket,
    MarketEventAnnotation,
    MarketSnapshot,
)
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService


BASE_TIME = datetime(2026, 9, 12, 14, 0, tzinfo=UTC)


def snapshot(
    minute: int,
    probability: str,
    *,
    liquidity: str = "500",
    family: MarketFamily = MarketFamily.MATCH_RESULT,
) -> MarketSnapshot:
    implied = Decimal(probability)
    return MarketSnapshot(
        observed_at=BASE_TIME + timedelta(minutes=minute),
        venue=VenueName.MATCHBOOK,
        canonical_event_id="evt:newcastle-arsenal",
        canonical_market_id=f"mkt:{family.value}",
        canonical_outcome="home",
        market_family=family,
        competition="Premier League",
        home_team="Newcastle United",
        away_team="Arsenal",
        source_event_id="123",
        source_market_id="456",
        source_outcome_id="789",
        kickoff_utc=BASE_TIME + timedelta(hours=2),
        decimal_odds=Decimal("1") / implied,
        implied_probability=implied,
        best_back_odds=Decimal("2.40"),
        best_lay_odds=Decimal("2.44"),
        back_size=Decimal("180"),
        lay_size=Decimal("160"),
        total_liquidity=Decimal(liquidity),
        order_book=[{"price": "2.40", "size": "180"}],
    )


def test_snapshot_derives_probability_spread_and_kickoff_bucket() -> None:
    item = MarketSnapshot(
        observed_at=BASE_TIME,
        venue=VenueName.MATCHBOOK,
        canonical_event_id="evt:1",
        canonical_market_id="mkt:1",
        canonical_outcome="over",
        market_family=MarketFamily.CORNERS,
        kickoff_utc=BASE_TIME + timedelta(minutes=90),
        decimal_odds=Decimal("2"),
        best_back_odds=Decimal("1.98"),
        best_lay_odds=Decimal("2.02"),
    )

    assert item.implied_probability == Decimal("0.5")
    assert item.spread_decimal == Decimal("0.04")
    assert item.minutes_to_kickoff == 90.0
    assert MarketIntelligenceAnalytics.kickoff_bucket(item) == KickoffBucket.H2_TO_M45


def test_repository_persists_snapshots_annotations_and_cohort_filters(tmp_path) -> None:
    repository = SqliteMarketIntelligenceRepository(tmp_path / "market-intelligence.sqlite")
    first = snapshot(0, "0.40")
    second = snapshot(5, "0.44", family=MarketFamily.CORNERS)
    repository.append_snapshot(first)
    repository.append_snapshot(second)

    annotation = MarketEventAnnotation(
        canonical_event_id=first.canonical_event_id,
        occurred_at=BASE_TIME + timedelta(minutes=3),
        category=AnnotationCategory.TEAM_SHEET,
        source="club",
        title="Starting XI announced",
        source_url="https://example.test/team-sheet",
    )
    repository.append_annotation(annotation)

    team_rows = repository.list_snapshots(team="Newcastle United")
    corner_rows = repository.list_snapshots(market_family=MarketFamily.CORNERS)
    annotations = repository.list_annotations(
        canonical_event_id=first.canonical_event_id,
        category=AnnotationCategory.TEAM_SHEET,
    )

    assert [row.snapshot_id for row in team_rows] == [first.snapshot_id, second.snapshot_id]
    assert [row.snapshot_id for row in corner_rows] == [second.snapshot_id]
    assert annotations[0].title == "Starting XI announced"
    assert annotations[0].source_url == "https://example.test/team-sheet"
    repository.close()


def test_movement_score_handles_sample_size_and_thin_liquidity() -> None:
    analytics = MarketIntelligenceAnalytics()
    cohort = [Decimal(value) for value in [
        "-0.02",
        "-0.01",
        "-0.005",
        "0.000",
        "0.005",
        "0.010",
        "0.015",
        "0.020",
        "0.025",
        "0.030",
    ]]
    score = analytics.score_move(
        Decimal("0.04"),
        cohort,
        current_liquidity=Decimal("20"),
    )

    assert score.sufficient_sample is True
    assert score.percentile == 100.0
    assert score.robust_z_score is not None
    assert score.thin_liquidity is True

    insufficient = analytics.score_move(Decimal("0.04"), cohort[:3])
    assert insufficient.sufficient_sample is False
    assert insufficient.percentile is None


def test_probability_move_and_cohort_stats() -> None:
    analytics = MarketIntelligenceAnalytics()
    history = [
        snapshot(0, "0.40"),
        snapshot(5, "0.42"),
        snapshot(15, "0.47"),
    ]

    move = analytics.probability_move(history, lookback_minutes=10)
    stats = analytics.cohort_stats([1, 2, 3, 4, 5, 6, 7, 8])

    assert move == Decimal("0.05")
    assert stats.sample_size == 8
    assert stats.median == 4.5
    assert stats.p25 == 2.75
    assert stats.p75 == 6.25
    assert stats.sufficient_sample is True


def test_annotation_reaction_reports_reversion_path() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = MarketIntelligenceService(repository)
    observations = [
        snapshot(0, "0.40"),
        snapshot(1, "0.50"),
        snapshot(2, "0.49"),
        snapshot(6, "0.45"),
        snapshot(16, "0.42"),
    ]
    for item in observations:
        service.record_snapshot(item)

    annotation = MarketEventAnnotation(
        canonical_event_id="evt:newcastle-arsenal",
        occurred_at=BASE_TIME,
        category=AnnotationCategory.TEAM_SHEET,
        source="club",
        title="Starting XI announced",
    )
    service.record_annotation(annotation)

    analysis = service.analyze_annotation_reaction(
        annotation=annotation,
        canonical_market_id="mkt:match_result",
        canonical_outcome="home",
        shock_window_minutes=2,
        horizons_minutes=(1, 5, 15),
    )

    assert analysis is not None
    assert analysis.baseline_probability == Decimal("0.40")
    assert analysis.shock_probability == Decimal("0.50")
    assert analysis.initial_move_probability_points == Decimal("0.10")
    assert analysis.points[0].classification == "reversion"
    assert analysis.points[1].retracement_fraction == 0.5
    assert round(analysis.points[2].retracement_fraction or 0, 6) == 0.8
    repository.close()


def test_cards_and_player_props_are_first_class_market_families() -> None:
    assert MarketFamily.CORNERS.value == "corners"
    assert MarketFamily.CARDS.value == "cards"
    assert MarketFamily.PLAYER_PROPS.value == "player_props"
