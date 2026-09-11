from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.models import VenueName


class ExecutableQuote(BaseModel):
    """One executable outcome quote after upstream fee/FX normalization.

    `net_decimal_odds` is the effective return multiplier used by the solver after
    known deterministic per-leg costs. Liquidity is expressed as maximum stake at
    this effective price/depth slice.
    """

    outcome: str
    venue: VenueName
    source_market_id: str
    net_decimal_odds: Decimal = Field(gt=Decimal("1"))
    max_stake: Decimal = Field(gt=Decimal("0"))


class ArbitrageStake(BaseModel):
    outcome: str
    venue: VenueName
    source_market_id: str
    stake: Decimal
    net_decimal_odds: Decimal
    state_return: Decimal


class ArbitrageSolution(BaseModel):
    is_arbitrage: bool
    implied_probability_sum: Decimal
    total_stake: Decimal = Decimal("0")
    guaranteed_return: Decimal = Decimal("0")
    guaranteed_profit: Decimal = Decimal("0")
    roi: Decimal = Decimal("0")
    stakes: list[ArbitrageStake] = Field(default_factory=list)
    rejection_reason: str | None = None


class PayoffLeg(BaseModel):
    """Generalized future-facing leg represented as P&L per unit stake by state."""

    leg_id: str
    venue: VenueName
    max_stake: Decimal = Field(gt=Decimal("0"))
    payoff_per_unit: dict[str, Decimal]

    @model_validator(mode="after")
    def ensure_states(self) -> "PayoffLeg":
        if not self.payoff_per_unit:
            raise ValueError("payoff_per_unit must contain at least one state")
        return self


class PayoffProblem(BaseModel):
    """General state/payoff representation for later synthetic/back-lay solving."""

    states: list[str]
    legs: list[PayoffLeg]
    capital_limit: Decimal | None = Field(default=None, gt=Decimal("0"))

    @model_validator(mode="after")
    def validate_state_coverage(self) -> "PayoffProblem":
        required = set(self.states)
        if not required:
            raise ValueError("states must not be empty")
        for leg in self.legs:
            if set(leg.payoff_per_unit) != required:
                raise ValueError(f"leg {leg.leg_id} does not cover the complete state space")
        return self
