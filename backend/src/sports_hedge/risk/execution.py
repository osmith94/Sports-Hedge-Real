from __future__ import annotations

from pydantic import BaseModel, Field


class ExecutionRiskInputs(BaseModel):
    spread_bps: float = Field(ge=0)
    size_to_depth_ratio: float = Field(ge=0)
    quote_age_ms: int = Field(ge=0)
    recent_volatility_bps: float = Field(ge=0)
    leg_count: int = Field(ge=2)
    minutes_to_kickoff: float = Field(ge=0)
    assumed_latency_ms: int = Field(ge=0)
    hedge_liquidity_ratio: float = Field(default=1.0, ge=0)


class ExecutionRiskResult(BaseModel):
    score: int = Field(ge=0, le=100)
    band: str
    reasons: list[str]


class ExecutionRiskScorer:
    """Deterministic Phase 1 heuristic, intentionally isolated for later calibration."""

    def score(self, data: ExecutionRiskInputs) -> ExecutionRiskResult:
        score = 0.0
        reasons: list[str] = []

        spread_component = min(data.spread_bps / 50.0, 1.0) * 15
        if spread_component >= 7.5:
            reasons.append("wide_spread")
        score += spread_component

        depth_component = min(data.size_to_depth_ratio, 1.5) / 1.5 * 25
        if data.size_to_depth_ratio >= 0.5:
            reasons.append("large_relative_to_depth")
        score += depth_component

        age_component = min(data.quote_age_ms / 3000.0, 1.0) * 15
        if data.quote_age_ms >= 1000:
            reasons.append("stale_quote")
        score += age_component

        volatility_component = min(data.recent_volatility_bps / 100.0, 1.0) * 15
        if data.recent_volatility_bps >= 40:
            reasons.append("recent_volatility")
        score += volatility_component

        extra_legs = max(data.leg_count - 2, 0)
        leg_component = min(extra_legs / 3.0, 1.0) * 10
        if extra_legs:
            reasons.append("multi_leg_execution")
        score += leg_component

        latency_component = min(data.assumed_latency_ms / 2000.0, 1.0) * 10
        if data.assumed_latency_ms >= 500:
            reasons.append("cross_venue_latency")
        score += latency_component

        if data.minutes_to_kickoff <= 10:
            score += 5
            reasons.append("near_kickoff")

        if data.hedge_liquidity_ratio < 1:
            hedge_component = min(1 - data.hedge_liquidity_ratio, 1.0) * 10
            score += hedge_component
            reasons.append("limited_hedge_liquidity")

        final_score = max(0, min(100, round(score)))
        if final_score <= 20:
            band = "low"
        elif final_score <= 40:
            band = "moderate"
        elif final_score <= 60:
            band = "elevated"
        elif final_score <= 80:
            band = "high"
        else:
            band = "extreme"

        return ExecutionRiskResult(score=final_score, band=band, reasons=reasons)
