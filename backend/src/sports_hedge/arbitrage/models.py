from __future__ import annotations

from decimal import Decimal
from typing import Any

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
    """One executable leg with an audited net P&L vector per settlement state.

    `payoff_per_unit` is net P&L per unit *stake* after upstream fee/FX
    normalization. `capital_per_unit` is capital consumed per unit stake (1 for
    a normal back/buy; future lay/liability legs must set this explicitly).
    """

    leg_id: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str | None = None
    runner_outcome: str | None = None
    max_stake: Decimal = Field(gt=Decimal("0"))
    capital_per_unit: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))
    payoff_per_unit: dict[str, Decimal]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_states(self) -> "PayoffLeg":
        if not self.payoff_per_unit:
            raise ValueError("payoff_per_unit must contain at least one state")
        for state, payoff in self.payoff_per_unit.items():
            if not state:
                raise ValueError("payoff_per_unit keys must be non-empty state ids")
            if payoff.is_nan() or payoff.is_infinite():
                raise ValueError(f"leg {self.leg_id} has a non-finite payoff in state {state}")
        return self


class PayoffProblem(BaseModel):
    """Complete state/payoff representation for the generalized max-min solver."""

    states: list[str]
    legs: list[PayoffLeg]
    capital_limit: Decimal | None = Field(default=None, gt=Decimal("0"))
    venue_capital_limits: dict[VenueName, Decimal] | None = None

    @model_validator(mode="after")
    def validate_state_coverage(self) -> "PayoffProblem":
        if not self.states:
            raise ValueError("states must not be empty")
        if len(self.states) != len(set(self.states)):
            raise ValueError("states must not contain duplicates")
        if not self.legs:
            raise ValueError("legs must not be empty")
        required = set(self.states)
        seen_ids: set[str] = set()
        for leg in self.legs:
            if leg.leg_id in seen_ids:
                raise ValueError(f"duplicate leg id {leg.leg_id}")
            seen_ids.add(leg.leg_id)
            covered = set(leg.payoff_per_unit)
            if covered != required:
                missing = required - covered
                extra = covered - required
                raise ValueError(
                    f"leg {leg.leg_id} does not cover the complete state space"
                    f" (missing={sorted(missing)} extra={sorted(extra)})"
                )
        if self.venue_capital_limits:
            for venue, limit in self.venue_capital_limits.items():
                if limit < 0:
                    raise ValueError("venue_capital_limits must be non-negative")
                del venue
        return self


class PayoffStake(BaseModel):
    leg_id: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str | None = None
    runner_outcome: str | None = None
    stake: Decimal
    capital_consumed: Decimal
    capital_per_unit: Decimal


class PayoffSolution(BaseModel):
    """Deterministic generalized solver result. LP output is never trusted as truth.

    `is_arbitrage` is true only after Decimal post-validation proves every state's
    net P&L is strictly positive. Minimum P&L of zero or less is a no-arbitrage path.
    """

    is_arbitrage: bool
    selected_stakes: list[PayoffStake] = Field(default_factory=list)
    total_capital_used: Decimal = Decimal("0")
    state_pnl: dict[str, Decimal] = Field(default_factory=dict)
    minimum_state_pnl: Decimal = Decimal("0")
    roi: Decimal = Decimal("0")
    rejection_reason: str | None = None
    numerically_validated: bool = False
    solver_engine: str = "scipy.linprog.highs"
