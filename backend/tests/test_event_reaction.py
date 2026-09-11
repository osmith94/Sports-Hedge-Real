from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.event_reaction import (
    EventReactionAnalyzer,
    MarketDependencyGraph,
)
from sports_hedge.market_intelligence.models import (
    AnnotationCategory,
    MarketEventAnnotation,
    MarketSnapshot,
)


EVENT_TIME = datetime(2026, 9, 12, 17, 45, tzinfo=UTC)


def make_snapshot(
    *,
    family: MarketFamily,
    seconds: int,
    probability: str,
    venue: VenueName = VenueName.MATCHBOOK,
    spread: str = "0.02",
    liquidity: str = "500",
) -> MarketSnapshot:
    implied = Decimal(probability)
    return MarketSnapshot(
        observed_at=EVENT_TIME + timedelta(seconds=seconds),
        venue=venue,
        canonical_event_id="evt:team-sheet-test",
        canonical_market_id=f"mkt:{family.value}",
        canonical_outcome="primary",
        market_family=family,
        competition="Premier League",
        home_team="Newcastle United",
        away_team="Arsenal",
        decimal_odds=Decimal("1") / implied,
        implied_probability=implied,
        spread_decimal=Decimal(spread),
        total_liquidity=Decimal(liquidity),
    )


def team_sheet_annotation() -> MarketEventAnnotation:
    return MarketEventAnnotation(
        canonical_event_id="evt:team-sheet-test",
        occurred_at=EVENT_TIME,
        category=AnnotationCategory.TEAM_SHEET,
        source="club",
        title="Starting XI announced",
    )


def event_snapshots() -> list[MarketSnapshot]:
    return [
        make_snapshot(family=MarketFamily.MATCH_RESULT, seconds=-60, probability="0.40"),
        make_snapshot(family=MarketFamily.MATCH_RESULT, seconds=10, probability="0.42"),
        make_snapshot(family=MarketFamily.MATCH_RESULT, seconds=60, probability="0.45"),
        make_snapshot(family=MarketFamily.MATCH_RESULT, seconds=300, probability="0.43"),
        make_snapshot(family=MarketFamily.PLAYER_PROPS, seconds=-60, probability="0.30"),
        make_snapshot(family=MarketFamily.PLAYER_PROPS, seconds=20, probability="0.32"),
        make_snapshot(family=MarketFamily.PLAYER_PROPS, seconds=90, probability="0.36"),
        make_snapshot(family=MarketFamily.PLAYER_PROPS, seconds=300, probability="0.34"),
        make_snapshot(family=MarketFamily.CORNERS, seconds=-60, probability="0.50"),
        make_snapshot(family=MarketFamily.CORNERS, seconds=40, probability="0.51"),
        make_snapshot(family=MarketFamily.CORNERS, seconds=120, probability="0.55"),
        make_snapshot(family=MarketFamily.CORNERS, seconds=300, probability="0.52"),
    ]


def test_team_sheet_reaction_orders_related_markets_by_first_response() -> None:
    analyzer = EventReactionAnalyzer()
    analysis = analyzer.analyze(
        team_sheet_annotation(),
        event_snapshots(),
        post_window_minutes=10,
        response_threshold_probability_points=Decimal("0.015"),
    )

    assert MarketFamily.CORNERS in analysis.expected_market_families
    assert MarketFamily.CARDS in analysis.expected_market_families
    assert MarketFamily.PLAYER_PROPS in analysis.expected_market_families
    assert [reaction.market_family for reaction in analysis.reactions] == [
        MarketFamily.MATCH_RESULT,
        MarketFamily.PLAYER_PROPS,
        MarketFamily.CORNERS,
    ]
    assert [reaction.first_response_seconds for reaction in analysis.reactions] == [10, 20, 120]

    match_reaction = analysis.reactions[0]
    assert match_reaction.peak_move_probability_points == Decimal("0.05")
    assert round(match_reaction.retracement_fraction or 0, 6) == 0.4


def test_lead_lag_aggregates_repeated_event_reactions() -> None:
    analyzer = EventReactionAnalyzer()
    analyses = [
        analyzer.analyze(
            team_sheet_annotation(),
            event_snapshots(),
            post_window_minutes=10,
            response_threshold_probability_points=Decimal("0.015"),
        )
        for _ in range(5)
    ]

    results = analyzer.lead_lag(analyses, minimum_sample_size=5)
    match_vs_corners = next(
        result
        for result in results
        if result.leading_family == MarketFamily.MATCH_RESULT
        and result.lagging_family == MarketFamily.CORNERS
    )

    assert match_vs_corners.median_lead_seconds == 110
    assert match_vs_corners.sample_size == 5
    assert match_vs_corners.sufficient_sample is True


def test_dependency_graph_is_configurable_for_yellow_cards_and_news() -> None:
    graph = MarketDependencyGraph()

    assert graph.families_for(AnnotationCategory.YELLOW_CARD) == {
        MarketFamily.CARDS,
        MarketFamily.PLAYER_PROPS,
    }
    assert MarketFamily.PLAYER_PROPS in graph.families_for(AnnotationCategory.NEWS_ARTICLE)

    graph.set_families(AnnotationCategory.YELLOW_CARD, {MarketFamily.CARDS})
    assert graph.families_for(AnnotationCategory.YELLOW_CARD) == {MarketFamily.CARDS}


def test_same_market_on_two_venues_is_not_blended() -> None:
    analyzer = EventReactionAnalyzer()
    snapshots = [
        make_snapshot(
            family=MarketFamily.MATCH_RESULT,
            seconds=-60,
            probability="0.40",
            venue=VenueName.MATCHBOOK,
        ),
        make_snapshot(
            family=MarketFamily.MATCH_RESULT,
            seconds=10,
            probability="0.43",
            venue=VenueName.MATCHBOOK,
        ),
        make_snapshot(
            family=MarketFamily.MATCH_RESULT,
            seconds=-60,
            probability="0.41",
            venue=VenueName.POLYMARKET,
        ),
        make_snapshot(
            family=MarketFamily.MATCH_RESULT,
            seconds=40,
            probability="0.44",
            venue=VenueName.POLYMARKET,
        ),
    ]

    analysis = analyzer.analyze(
        team_sheet_annotation(),
        snapshots,
        post_window_minutes=5,
        response_threshold_probability_points=Decimal("0.02"),
    )

    assert len(analysis.reactions) == 2
    by_venue = {reaction.venue: reaction for reaction in analysis.reactions}
    assert by_venue[VenueName.MATCHBOOK].first_response_seconds == 10
    assert by_venue[VenueName.POLYMARKET].first_response_seconds == 40
