from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Iterable, Sequence

from pydantic import BaseModel, Field

from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import (
    AnnotationCategory,
    MarketEventAnnotation,
    MarketSnapshot,
)


class MarketReaction(BaseModel):
    venue: VenueName
    market_family: MarketFamily
    canonical_market_id: str
    canonical_outcome: str
    baseline_probability: Decimal
    first_response_seconds: float | None = None
    peak_move_probability_points: Decimal
    time_to_peak_seconds: float | None = None
    end_move_probability_points: Decimal
    retracement_fraction: float | None = None
    spread_change: Decimal | None = None
    liquidity_change: Decimal | None = None
    observation_count: int = Field(ge=0)


class EventReactionAnalysis(BaseModel):
    annotation_id: str
    canonical_event_id: str
    category: AnnotationCategory
    occurred_at: datetime
    expected_market_families: list[MarketFamily]
    reactions: list[MarketReaction] = Field(default_factory=list)


class LeadLagResult(BaseModel):
    leading_family: MarketFamily
    lagging_family: MarketFamily
    median_lead_seconds: float
    sample_size: int = Field(ge=1)
    sufficient_sample: bool


class MarketDependencyGraph:
    """Configurable map of event types to market families expected to react."""

    def __init__(
        self,
        mapping: dict[AnnotationCategory, set[MarketFamily]] | None = None,
    ) -> None:
        self._mapping = mapping or default_dependency_mapping()

    def families_for(self, category: AnnotationCategory) -> set[MarketFamily]:
        return set(self._mapping.get(category, set()))

    def set_families(
        self,
        category: AnnotationCategory,
        families: Iterable[MarketFamily],
    ) -> None:
        self._mapping[category] = set(families)


class EventReactionAnalyzer:
    def __init__(self, dependency_graph: MarketDependencyGraph | None = None) -> None:
        self.dependency_graph = dependency_graph or MarketDependencyGraph()

    def analyze(
        self,
        annotation: MarketEventAnnotation,
        snapshots: Sequence[MarketSnapshot],
        *,
        pre_window_minutes: int = 5,
        post_window_minutes: int = 30,
        response_threshold_probability_points: Decimal = Decimal("0.005"),
    ) -> EventReactionAnalysis:
        expected = self.dependency_graph.families_for(annotation.category)
        grouped: dict[tuple[VenueName, str, str], list[MarketSnapshot]] = defaultdict(list)
        start = annotation.occurred_at - timedelta(minutes=pre_window_minutes)
        end = annotation.occurred_at + timedelta(minutes=post_window_minutes)

        for snapshot in snapshots:
            if snapshot.canonical_event_id != annotation.canonical_event_id:
                continue
            if not start <= snapshot.observed_at <= end:
                continue
            grouped[
                (snapshot.venue, snapshot.canonical_market_id, snapshot.canonical_outcome)
            ].append(snapshot)

        reactions: list[MarketReaction] = []
        for observations in grouped.values():
            ordered = sorted(observations, key=lambda item: item.observed_at)
            baseline_candidates = [
                item for item in ordered if item.observed_at <= annotation.occurred_at
            ]
            post = [item for item in ordered if item.observed_at > annotation.occurred_at]
            if not baseline_candidates or not post:
                continue

            baseline = baseline_candidates[-1]
            baseline_probability = _probability(baseline)
            peak = max(
                post,
                key=lambda item: abs(_probability(item) - baseline_probability),
            )
            peak_move = _probability(peak) - baseline_probability
            final = post[-1]
            end_move = _probability(final) - baseline_probability
            first_response = next(
                (
                    item
                    for item in post
                    if abs(_probability(item) - baseline_probability)
                    >= response_threshold_probability_points
                ),
                None,
            )
            retracement = _retracement_fraction(peak_move, end_move)
            reactions.append(
                MarketReaction(
                    venue=baseline.venue,
                    market_family=baseline.market_family,
                    canonical_market_id=baseline.canonical_market_id,
                    canonical_outcome=baseline.canonical_outcome,
                    baseline_probability=baseline_probability,
                    first_response_seconds=(
                        (first_response.observed_at - annotation.occurred_at).total_seconds()
                        if first_response is not None
                        else None
                    ),
                    peak_move_probability_points=peak_move,
                    time_to_peak_seconds=(peak.observed_at - annotation.occurred_at).total_seconds(),
                    end_move_probability_points=end_move,
                    retracement_fraction=retracement,
                    spread_change=_change(baseline.spread_decimal, final.spread_decimal),
                    liquidity_change=_change(baseline.total_liquidity, final.total_liquidity),
                    observation_count=len(ordered),
                )
            )

        reactions.sort(
            key=lambda reaction: (
                reaction.first_response_seconds is None,
                reaction.first_response_seconds or float("inf"),
                reaction.venue.value,
                reaction.market_family.value,
            )
        )
        return EventReactionAnalysis(
            annotation_id=annotation.annotation_id,
            canonical_event_id=annotation.canonical_event_id,
            category=annotation.category,
            occurred_at=annotation.occurred_at,
            expected_market_families=sorted(expected, key=lambda family: family.value),
            reactions=reactions,
        )

    @staticmethod
    def lead_lag(
        analyses: Sequence[EventReactionAnalysis],
        *,
        minimum_sample_size: int = 5,
    ) -> list[LeadLagResult]:
        pair_deltas: dict[tuple[MarketFamily, MarketFamily], list[float]] = defaultdict(list)
        for analysis in analyses:
            by_family: dict[MarketFamily, float] = {}
            for reaction in analysis.reactions:
                if reaction.first_response_seconds is None:
                    continue
                current = by_family.get(reaction.market_family)
                if current is None or reaction.first_response_seconds < current:
                    by_family[reaction.market_family] = reaction.first_response_seconds

            families = sorted(by_family, key=lambda item: item.value)
            for index, first in enumerate(families):
                for second in families[index + 1 :]:
                    delta = by_family[second] - by_family[first]
                    if delta >= 0:
                        pair_deltas[(first, second)].append(delta)
                    else:
                        pair_deltas[(second, first)].append(-delta)

        results = [
            LeadLagResult(
                leading_family=leader,
                lagging_family=lagger,
                median_lead_seconds=median(deltas),
                sample_size=len(deltas),
                sufficient_sample=len(deltas) >= minimum_sample_size,
            )
            for (leader, lagger), deltas in pair_deltas.items()
        ]
        return sorted(
            results,
            key=lambda item: (-item.median_lead_seconds, item.leading_family.value),
        )


def default_dependency_mapping() -> dict[AnnotationCategory, set[MarketFamily]]:
    return {
        AnnotationCategory.TEAM_SHEET: {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.BOTH_TEAMS_TO_SCORE,
            MarketFamily.ASIAN_HANDICAP,
            MarketFamily.CORNERS,
            MarketFamily.CARDS,
            MarketFamily.PLAYER_PROPS,
        },
        AnnotationCategory.PLAYER_OUT: {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.CORNERS,
            MarketFamily.PLAYER_PROPS,
        },
        AnnotationCategory.PLAYER_IN: {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.CORNERS,
            MarketFamily.PLAYER_PROPS,
        },
        AnnotationCategory.RED_CARD: {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.NEXT_GOAL,
            MarketFamily.ASIAN_HANDICAP,
            MarketFamily.CORNERS,
            MarketFamily.CARDS,
        },
        AnnotationCategory.YELLOW_CARD: {
            MarketFamily.CARDS,
            MarketFamily.PLAYER_PROPS,
        },
        AnnotationCategory.GOAL: {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.NEXT_GOAL,
            MarketFamily.ASIAN_HANDICAP,
            MarketFamily.CORNERS,
        },
        AnnotationCategory.INJURY_NEWS: {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.PLAYER_PROPS,
        },
        AnnotationCategory.CLUB_ANNOUNCEMENT: {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.PLAYER_PROPS,
        },
        AnnotationCategory.NEWS_ARTICLE: {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.PLAYER_PROPS,
        },
    }


def _probability(snapshot: MarketSnapshot) -> Decimal:
    if snapshot.implied_probability is None:
        raise ValueError("Snapshot has no implied probability")
    return snapshot.implied_probability


def _retracement_fraction(peak_move: Decimal, end_move: Decimal) -> float | None:
    if peak_move == 0:
        return None
    return float((peak_move - end_move) / peak_move)


def _change(before: Decimal | None, after: Decimal | None) -> Decimal | None:
    if before is None or after is None:
        return None
    return after - before
