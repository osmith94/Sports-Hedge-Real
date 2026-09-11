from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal
from statistics import median
from typing import Iterable, Sequence

from sports_hedge.market_intelligence.models import (
    HistoricalCohortStats,
    KickoffBucket,
    MarketSnapshot,
    MovementScore,
    ReversionAnalysis,
    ReversionPoint,
)


class MarketIntelligenceAnalytics:
    """Pure analytics over canonical historical market observations."""

    @staticmethod
    def kickoff_bucket(snapshot: MarketSnapshot) -> KickoffBucket:
        minutes = snapshot.minutes_to_kickoff
        if minutes is None:
            return KickoffBucket.UNKNOWN
        if minutes < 0:
            return KickoffBucket.IN_PLAY
        if minutes <= 45:
            return KickoffBucket.M45_TO_KICKOFF
        if minutes <= 120:
            return KickoffBucket.H2_TO_M45
        if minutes <= 180:
            return KickoffBucket.H3_TO_H2
        if minutes <= 1440:
            return KickoffBucket.H24_TO_H3
        return KickoffBucket.OVER_24H

    @staticmethod
    def probability_move(
        snapshots: Sequence[MarketSnapshot],
        *,
        lookback_minutes: int,
    ) -> Decimal | None:
        if lookback_minutes <= 0 or len(snapshots) < 2:
            return None
        ordered = sorted(snapshots, key=lambda item: item.observed_at)
        current = ordered[-1]
        target = current.observed_at - timedelta(minutes=lookback_minutes)
        eligible = [snapshot for snapshot in ordered[:-1] if snapshot.observed_at <= target]
        if not eligible:
            return None
        baseline = eligible[-1]
        return _probability(current) - _probability(baseline)

    @staticmethod
    def logit(probability: Decimal) -> float:
        value = float(probability)
        if not 0 < value < 1:
            raise ValueError("Probability must be strictly between 0 and 1")
        return math.log(value / (1.0 - value))

    @staticmethod
    def logit_move(first: MarketSnapshot, second: MarketSnapshot) -> float:
        return MarketIntelligenceAnalytics.logit(
            _probability(second)
        ) - MarketIntelligenceAnalytics.logit(_probability(first))

    @staticmethod
    def score_move(
        move_probability_points: Decimal,
        cohort_moves: Sequence[Decimal | float],
        *,
        minimum_sample_size: int = 8,
        current_liquidity: Decimal | None = None,
        thin_liquidity_threshold: Decimal = Decimal("50"),
    ) -> MovementScore:
        values = [float(value) for value in cohort_moves]
        sample_size = len(values)
        sufficient = sample_size >= minimum_sample_size
        thin_liquidity = (
            current_liquidity is not None and current_liquidity < thin_liquidity_threshold
        )
        if not sufficient:
            return MovementScore(
                sample_size=sample_size,
                move_probability_points=move_probability_points,
                sufficient_sample=False,
                thin_liquidity=thin_liquidity,
            )

        move = float(move_probability_points)
        magnitude = abs(move)
        percentile = 100.0 * sum(abs(value) <= magnitude for value in values) / sample_size
        centre = median(values)
        deviations = [abs(value - centre) for value in values]
        mad = median(deviations)
        robust_z = None if mad == 0 else 0.67448975 * (move - centre) / mad
        return MovementScore(
            sample_size=sample_size,
            move_probability_points=move_probability_points,
            percentile=percentile,
            robust_z_score=robust_z,
            sufficient_sample=True,
            thin_liquidity=thin_liquidity,
        )

    @staticmethod
    def cohort_stats(
        values: Iterable[Decimal | float],
        *,
        minimum_sample_size: int = 8,
    ) -> HistoricalCohortStats:
        data = sorted(float(value) for value in values)
        sample_size = len(data)
        if not data:
            return HistoricalCohortStats(sample_size=0, sufficient_sample=False)
        return HistoricalCohortStats(
            sample_size=sample_size,
            median=median(data),
            p25=_percentile(data, 25.0),
            p75=_percentile(data, 75.0),
            minimum=data[0],
            maximum=data[-1],
            sufficient_sample=sample_size >= minimum_sample_size,
        )

    @staticmethod
    def reversion_analysis(
        *,
        baseline_probability: Decimal,
        shock_snapshot: MarketSnapshot,
        post_snapshots: Sequence[MarketSnapshot],
        horizons_minutes: Sequence[int] = (1, 5, 15, 30, 60),
    ) -> ReversionAnalysis:
        shock_probability = _probability(shock_snapshot)
        initial_move = shock_probability - baseline_probability
        if initial_move == 0:
            raise ValueError("Shock probability must differ from the baseline probability")

        ordered = sorted(
            (snapshot for snapshot in post_snapshots if snapshot.observed_at >= shock_snapshot.observed_at),
            key=lambda item: item.observed_at,
        )
        points: list[ReversionPoint] = []
        for horizon in horizons_minutes:
            if horizon <= 0:
                raise ValueError("Reversion horizons must be positive")
            target = shock_snapshot.observed_at + timedelta(minutes=horizon)
            snapshot = _nearest_snapshot(ordered, target, horizon)
            if snapshot is None:
                points.append(ReversionPoint(horizon_minutes=horizon))
                continue

            observed = _probability(snapshot)
            retracement = float((shock_probability - observed) / initial_move)
            points.append(
                ReversionPoint(
                    horizon_minutes=horizon,
                    observed_probability=observed,
                    retracement_fraction=retracement,
                    classification=_classify_retracement(retracement),
                )
            )

        return ReversionAnalysis(
            baseline_probability=baseline_probability,
            shock_probability=shock_probability,
            shock_time=shock_snapshot.observed_at,
            initial_move_probability_points=initial_move,
            points=points,
        )


def _probability(snapshot: MarketSnapshot) -> Decimal:
    if snapshot.implied_probability is None:
        raise ValueError("Snapshot has no implied probability")
    return snapshot.implied_probability


def _nearest_snapshot(
    snapshots: Sequence[MarketSnapshot],
    target,
    horizon_minutes: int,
) -> MarketSnapshot | None:
    if not snapshots:
        return None
    candidate = min(snapshots, key=lambda item: abs((item.observed_at - target).total_seconds()))
    maximum_gap_seconds = max(60.0, horizon_minutes * 30.0)
    if abs((candidate.observed_at - target).total_seconds()) > maximum_gap_seconds:
        return None
    return candidate


def _classify_retracement(retracement: float) -> str:
    if retracement >= 0.10:
        return "reversion"
    if retracement <= -0.10:
        return "continuation"
    return "stabilization"


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("Cannot calculate a percentile for an empty sequence")
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * percentile / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[lower]
    fraction = rank - lower
    return values[lower] + (values[upper] - values[lower]) * fraction
