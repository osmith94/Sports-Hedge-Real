from __future__ import annotations

from decimal import Decimal

from sports_hedge.research.value.contracts import DataQuality, ScoreComponents

_QUALITY_WEIGHTS = {
    DataQuality.HIGH: Decimal("1"),
    DataQuality.MEDIUM: Decimal("0.70"),
    DataQuality.LOW: Decimal("0.30"),
    DataQuality.UNKNOWN: Decimal("0.40"),
}

_WEIGHTS = {
    "economic_edge": Decimal("0.35"),
    "sample_size": Decimal("0.15"),
    "confidence": Decimal("0.12"),
    "stability": Decimal("0.08"),
    "regime_relevance": Decimal("0.10"),
    "data_quality": Decimal("0.08"),
    "quote_freshness": Decimal("0.07"),
    "liquidity": Decimal("0.05"),
}


def _clip01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal("0")
    if value > 1:
        return Decimal("1")
    return value


def _unit(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return _clip01(numerator / denominator)


def value_signal_score(
    *,
    probability_edge_pp: Decimal,
    sample_size: int,
    min_sample_size: int,
    confidence: Decimal,
    stability: Decimal,
    regime_relevance: Decimal,
    data_quality: DataQuality,
    quote_age_seconds: Decimal,
    max_quote_age_seconds: Decimal,
    available_depth: Decimal,
    min_available_depth: Decimal,
) -> tuple[Decimal, ScoreComponents]:
    """Combine economic edge with reliability. SRC is not an input."""

    components = ScoreComponents(
        economic_edge=_unit(probability_edge_pp, Decimal("15")),
        sample_size=_unit(Decimal(sample_size), Decimal(min_sample_size)),
        confidence=_clip01(confidence),
        stability=_clip01(stability),
        regime_relevance=_clip01(regime_relevance),
        data_quality=_QUALITY_WEIGHTS[data_quality],
        quote_freshness=_clip01(
            Decimal("1") - _unit(quote_age_seconds, max_quote_age_seconds)
        ),
        liquidity=_unit(available_depth, min_available_depth),
    )
    total = (
        components.economic_edge * _WEIGHTS["economic_edge"]
        + components.sample_size * _WEIGHTS["sample_size"]
        + components.confidence * _WEIGHTS["confidence"]
        + components.stability * _WEIGHTS["stability"]
        + components.regime_relevance * _WEIGHTS["regime_relevance"]
        + components.data_quality * _WEIGHTS["data_quality"]
        + components.quote_freshness * _WEIGHTS["quote_freshness"]
        + components.liquidity * _WEIGHTS["liquidity"]
    )
    return total * Decimal("100"), components
