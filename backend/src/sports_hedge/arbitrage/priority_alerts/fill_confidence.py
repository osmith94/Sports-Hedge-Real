from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.priority_alerts.models import (
    FILL_CONFIDENCE_RANK,
    FillConfidence,
    FillConfidenceBreakdown,
    FillConfidenceInputs,
)


def score_fill_confidence(inputs: FillConfidenceInputs) -> FillConfidenceBreakdown:
    """Operational fill estimate. Visible inputs, never a fill guarantee."""

    score = 100.0
    reasons: list[str] = []

    if inputs.depth_coverage_ratio < 1:
        score -= 40
        reasons.append("depth_coverage_below_one")
    elif inputs.depth_coverage_ratio < Decimal("1.05"):
        score -= 10
        reasons.append("thin_depth_buffer")

    age_penalty = min(inputs.quote_age_ms / 2000.0, 1.0) * 25
    if inputs.quote_age_ms >= 1000:
        reasons.append("stale_quote")
    score -= age_penalty

    if inputs.quote_persistence < Decimal("0.8"):
        score -= 15
        reasons.append("weak_quote_persistence")
    elif inputs.quote_persistence < Decimal("1"):
        score -= 5

    extra_levels = max(inputs.levels_consumed - 2, 0)
    if extra_levels:
        score -= min(extra_levels, 4) * 5
        reasons.append("multiple_book_levels")

    latency_penalty = min(inputs.assumed_latency_ms / 2000.0, 1.0) * 10
    if inputs.assumed_latency_ms >= 500:
        reasons.append("cross_venue_latency")
    score -= latency_penalty

    if inputs.venue_cancellation_rate is not None and inputs.venue_cancellation_rate >= Decimal(
        "0.1"
    ):
        score -= 15
        reasons.append("elevated_cancellation_rate")

    if (
        inputs.historical_paper_fill_rate is not None
        and inputs.historical_paper_fill_rate < Decimal("0.8")
    ):
        score -= 10
        reasons.append("weak_historical_paper_fills")

    final = max(0, min(100, round(score)))
    if final >= 75:
        band = FillConfidence.HIGH
    elif final >= 50:
        band = FillConfidence.MEDIUM
    else:
        band = FillConfidence.LOW

    return FillConfidenceBreakdown(band=band, score=final, inputs=inputs, reasons=reasons)


def meets_minimum(actual: FillConfidence, required: FillConfidence) -> bool:
    return FILL_CONFIDENCE_RANK[actual] >= FILL_CONFIDENCE_RANK[required]
