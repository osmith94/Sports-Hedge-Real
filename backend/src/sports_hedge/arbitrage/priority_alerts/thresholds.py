from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from sports_hedge.arbitrage.priority_alerts.models import FillConfidence
from sports_hedge.domain.models import VenueName


class PriorityAlertThresholds(BaseModel):
    """Configurable Priority Alert gates. Ordinary arb can still exist below these."""

    minimum_net_edge: Decimal = Field(default=Decimal("0.03"), ge=0)
    minimum_expected_profit: Decimal = Field(default=Decimal("20"), ge=0)
    minimum_executable_depth: Decimal = Field(default=Decimal("200"), ge=0)
    maximum_quote_age_ms: int = Field(default=2000, ge=0)
    maximum_execution_risk: int = Field(default=40, ge=0, le=100)
    minimum_depth_coverage: Decimal = Field(default=Decimal("1.0"), ge=0)
    minimum_fill_confidence: FillConfidence = FillConfidence.MEDIUM
    minimum_capital_efficiency: Decimal = Field(default=Decimal("0.03"), ge=0)
    safety_haircut: Decimal = Field(default=Decimal("0.05"), ge=0, lt=1)
    operator_manual_cap: Decimal = Field(default=Decimal("10000"), gt=0)
    risk_limit: Decimal = Field(default=Decimal("10000"), gt=0)
    venue_limits: dict[VenueName, Decimal] = Field(default_factory=dict)
    high_priority_edge: Decimal = Field(default=Decimal("0.05"), ge=0)
    critical_edge: Decimal = Field(default=Decimal("0.08"), ge=0)
    critical_max_quote_age_ms: int = Field(default=500, ge=0)
    critical_max_execution_risk: int = Field(default=25, ge=0, le=100)
    high_priority_max_execution_risk: int = Field(default=35, ge=0, le=100)
    material_edge_delta: Decimal = Field(default=Decimal("0.001"), ge=0)
    material_size_ratio: Decimal = Field(default=Decimal("0.02"), ge=0)
    minimum_survivability_score: int | None = Field(default=None, ge=0, le=100)
    minimum_survival_probability_at_required_latency: Decimal | None = Field(
        default=None, ge=0, le=1
    )
    minimum_survival_probability_at_action_latency: Decimal | None = Field(
        default=None, ge=0, le=1
    )

    def survival_probability_floor(self) -> Decimal | None:
        if self.minimum_survival_probability_at_required_latency is not None:
            return self.minimum_survival_probability_at_required_latency
        return self.minimum_survival_probability_at_action_latency
    external_survivability_warn_probability: Decimal = Field(
        default=Decimal("0.50"), ge=0, le=1
    )
