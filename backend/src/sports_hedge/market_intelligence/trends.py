from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from pydantic import BaseModel, Field

from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.analytics import MarketIntelligenceAnalytics
from sports_hedge.market_intelligence.models import KickoffBucket, MarketSnapshot


class TrendMetric(StrEnum):
    OPEN_TO_CLOSE_PROBABILITY = "open_to_close_probability"
    REALIZED_LOGIT_VOLATILITY = "realized_logit_volatility"
    SPREAD_CHANGE = "spread_change"
    LIQUIDITY_GROWTH = "liquidity_growth"
    PEAK_RETRACEMENT = "peak_retracement"


class TrendQuery(BaseModel):
    venue: VenueName | None = None
    market_family: MarketFamily | None = None
    period: FootballPeriod | None = None
    market_line: Decimal | None = None
    settlement_scope: SettlementScope | None = None
    competition: str | None = None
    team: str | None = None
    canonical_outcome: str | None = None
    kickoff_bucket: KickoffBucket | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    minimum_start_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    maximum_start_probability: float | None = Field(default=None, ge=0.0, le=1.0)


class TrendObservation(BaseModel):
    metric: TrendMetric
    canonical_event_id: str
    canonical_market_id: str
    canonical_outcome: str
    venue: VenueName
    market_family: MarketFamily
    period: FootballPeriod
    market_line: Decimal | None = None
    settlement_scope: SettlementScope
    value: float
    start_at: datetime
    end_at: datetime
    snapshot_count: int = Field(ge=2)


class TrendSummary(BaseModel):
    metric: TrendMetric
    sample_size: int = Field(ge=0)
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    positive_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    stability_iqr: float | None = Field(default=None, ge=0.0)
    sufficient_sample: bool
    observations: list[TrendObservation] = Field(default_factory=list)


class TrendExplorer:
    """Composable cohort analytics over historical canonical market snapshots."""

    def __init__(self, *, minimum_sample_size: int = 8) -> None:
        self.minimum_sample_size = minimum_sample_size
        self._analytics = MarketIntelligenceAnalytics()

    def analyze(
        self,
        metric: TrendMetric,
        snapshots: Sequence[MarketSnapshot],
        *,
        query: TrendQuery | None = None,
    ) -> TrendSummary:
        filtered = self.filter_snapshots(snapshots, query or TrendQuery())
        series = _group_series(filtered)
        observations: list[TrendObservation] = []
        for group in series.values():
            observation = self._observation(metric, group)
            if observation is not None:
                observations.append(observation)

        values = [item.value for item in observations]
        stats = self._analytics.cohort_stats(
            values,
            minimum_sample_size=self.minimum_sample_size,
        )
        positive_rate = None
        stability_iqr = None
        if values:
            positive_rate = sum(value > 0 for value in values) / len(values)
            if stats.p25 is not None and stats.p75 is not None:
                stability_iqr = stats.p75 - stats.p25

        return TrendSummary(
            metric=metric,
            sample_size=stats.sample_size,
            median=stats.median,
            p25=stats.p25,
            p75=stats.p75,
            minimum=stats.minimum,
            maximum=stats.maximum,
            positive_rate=positive_rate,
            stability_iqr=stability_iqr,
            sufficient_sample=stats.sufficient_sample,
            observations=sorted(observations, key=lambda item: item.start_at),
        )

    def filter_snapshots(
        self,
        snapshots: Sequence[MarketSnapshot],
        query: TrendQuery,
    ) -> list[MarketSnapshot]:
        result: list[MarketSnapshot] = []
        for snapshot in snapshots:
            if query.venue is not None and snapshot.venue != query.venue:
                continue
            if query.market_family is not None and snapshot.market_family != query.market_family:
                continue
            if query.period is not None and snapshot.period != query.period:
                continue
            if query.market_line is not None and snapshot.market_line != query.market_line:
                continue
            if query.settlement_scope is not None:
                if snapshot.settlement_scope != query.settlement_scope:
                    continue
            if query.competition is not None and snapshot.competition != query.competition:
                continue
            if query.team is not None and query.team not in {snapshot.home_team, snapshot.away_team}:
                continue
            if query.canonical_outcome is not None:
                if snapshot.canonical_outcome != query.canonical_outcome:
                    continue
            if query.kickoff_bucket is not None:
                if self._analytics.kickoff_bucket(snapshot) != query.kickoff_bucket:
                    continue
            if query.start_at is not None and snapshot.observed_at < query.start_at:
                continue
            if query.end_at is not None and snapshot.observed_at > query.end_at:
                continue
            result.append(snapshot)

        if query.minimum_start_probability is None and query.maximum_start_probability is None:
            return result

        allowed_keys: set[tuple[str, str, str, VenueName]] = set()
        for key, series in _group_series(result).items():
            first_probability = _probability(series[0])
            if (
                query.minimum_start_probability is not None
                and first_probability < query.minimum_start_probability
            ):
                continue
            if (
                query.maximum_start_probability is not None
                and first_probability > query.maximum_start_probability
            ):
                continue
            allowed_keys.add(key)

        return [snapshot for snapshot in result if _series_key(snapshot) in allowed_keys]

    def _observation(
        self,
        metric: TrendMetric,
        snapshots: Sequence[MarketSnapshot],
    ) -> TrendObservation | None:
        ordered = sorted(snapshots, key=lambda item: item.observed_at)
        if len(ordered) < 2:
            return None

        if metric == TrendMetric.OPEN_TO_CLOSE_PROBABILITY:
            value = _probability(ordered[-1]) - _probability(ordered[0])
        elif metric == TrendMetric.REALIZED_LOGIT_VOLATILITY:
            value = _realized_logit_volatility(ordered)
        elif metric == TrendMetric.SPREAD_CHANGE:
            value = _spread_change(ordered)
        elif metric == TrendMetric.LIQUIDITY_GROWTH:
            value = _liquidity_growth(ordered)
        elif metric == TrendMetric.PEAK_RETRACEMENT:
            value = _peak_retracement(ordered)
        else:  # pragma: no cover
            raise ValueError(f"Unsupported trend metric: {metric}")

        if value is None or not math.isfinite(value):
            return None
        first = ordered[0]
        return TrendObservation(
            metric=metric,
            canonical_event_id=first.canonical_event_id,
            canonical_market_id=first.canonical_market_id,
            canonical_outcome=first.canonical_outcome,
            venue=first.venue,
            market_family=first.market_family,
            period=first.period,
            market_line=first.market_line,
            settlement_scope=first.settlement_scope,
            value=value,
            start_at=ordered[0].observed_at,
            end_at=ordered[-1].observed_at,
            snapshot_count=len(ordered),
        )


def _group_series(
    snapshots: Sequence[MarketSnapshot],
) -> dict[tuple[str, str, str, VenueName], list[MarketSnapshot]]:
    grouped: dict[tuple[str, str, str, VenueName], list[MarketSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped[_series_key(snapshot)].append(snapshot)
    for series in grouped.values():
        series.sort(key=lambda item: item.observed_at)
    return grouped


def _series_key(snapshot: MarketSnapshot) -> tuple[str, str, str, VenueName]:
    return (
        snapshot.canonical_event_id,
        snapshot.canonical_market_id,
        snapshot.canonical_outcome,
        snapshot.venue,
    )


def _probability(snapshot: MarketSnapshot) -> float:
    if snapshot.implied_probability is None:
        raise ValueError("Snapshot has no implied probability")
    return float(snapshot.implied_probability)


def _realized_logit_volatility(snapshots: Sequence[MarketSnapshot]) -> float:
    probabilities = [snapshot.implied_probability for snapshot in snapshots]
    if any(probability is None for probability in probabilities):
        raise ValueError("Snapshot has no implied probability")
    logits = [
        MarketIntelligenceAnalytics.logit(probability)
        for probability in probabilities
        if probability is not None
    ]
    changes = [second - first for first, second in zip(logits, logits[1:])]
    return math.sqrt(sum(change * change for change in changes))


def _spread_change(snapshots: Sequence[MarketSnapshot]) -> float | None:
    first = snapshots[0].spread_decimal
    last = snapshots[-1].spread_decimal
    if first is None or last is None:
        return None
    return float(last - first)


def _liquidity_growth(snapshots: Sequence[MarketSnapshot]) -> float | None:
    first = snapshots[0].total_liquidity
    last = snapshots[-1].total_liquidity
    if first is None or last is None or first <= 0:
        return None
    return float((last - first) / first)


def _peak_retracement(snapshots: Sequence[MarketSnapshot]) -> float | None:
    baseline = _probability(snapshots[0])
    moves = [_probability(snapshot) - baseline for snapshot in snapshots[1:]]
    if not moves:
        return None
    peak_move = max(moves, key=abs)
    if peak_move == 0:
        return 0.0
    end_move = moves[-1]
    return (peak_move - end_move) / peak_move
