"""Scope-selected Min Net Arb. Qualification only; no venue writes.

FIXTURE_MATCH uses the existing fixture threshold. COMPETITION_SEASON uses
the separate owner-configured outright threshold. Missing outright config
fails closed and never inherits the fixture value. Lock duration and
annualisation are not inputs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.domain.models import MarketScope

FIXTURE_MIN_NET_EDGE_SOURCE = "min_net_edge"
OUTRIGHT_MIN_NET_EDGE_SOURCE = "outright_min_net_edge"
OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED = "outright_min_net_edge_unconfigured"


class MinNetThresholdDecision(BaseModel):
    """Auditable threshold chosen for one qualification/admission decision."""

    market_scope: MarketScope
    applied_threshold: Decimal | None = Field(default=None, ge=0)
    threshold_source: str
    configured: bool
    fail_closed_reason: str | None = None

    def as_decision_fields(self) -> dict[str, Any]:
        return {
            "minimum_net_edge": self.applied_threshold,
            "min_net_edge_scope": self.market_scope,
            "min_net_edge_source": self.threshold_source,
            "min_net_edge_configured": self.configured,
        }


def catalogue_market_scope(_identity: object | None = None) -> MarketScope:
    """Every current catalogue row remains FIXTURE_MATCH.

    This lane does not admit COMPETITION_SEASON rows or invent outright
    equivalence. Tests may still pass that scope into the resolver.
    """

    return MarketScope.FIXTURE_MATCH


def resolve_min_net_threshold(
    market_scope: MarketScope | str,
    *,
    fixture_min_net_edge: Decimal,
    outright_min_net_edge: Decimal | None,
) -> MinNetThresholdDecision:
    """Select Min Net Arb by canonical MarketScope only.

    COMPETITION_SEASON never reads ``fixture_min_net_edge``. An unset outright
    threshold is unconfigured and fails closed.
    """

    scope = MarketScope(market_scope)
    if scope is MarketScope.FIXTURE_MATCH:
        if fixture_min_net_edge < 0:
            raise ValueError("fixture_min_net_edge must be non-negative")
        return MinNetThresholdDecision(
            market_scope=scope,
            applied_threshold=fixture_min_net_edge,
            threshold_source=FIXTURE_MIN_NET_EDGE_SOURCE,
            configured=True,
            fail_closed_reason=None,
        )
    if outright_min_net_edge is None:
        return MinNetThresholdDecision(
            market_scope=scope,
            applied_threshold=None,
            threshold_source=OUTRIGHT_MIN_NET_EDGE_SOURCE,
            configured=False,
            fail_closed_reason=OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED,
        )
    if outright_min_net_edge < 0:
        raise ValueError("outright_min_net_edge must be non-negative")
    return MinNetThresholdDecision(
        market_scope=scope,
        applied_threshold=outright_min_net_edge,
        threshold_source=OUTRIGHT_MIN_NET_EDGE_SOURCE,
        configured=True,
        fail_closed_reason=None,
    )


def qualifies_with_resolved_threshold(
    current_net_edge: Decimal,
    threshold: MinNetThresholdDecision,
) -> bool:
    """True only when the resolved threshold is configured and the edge meets it."""

    if not threshold.configured or threshold.applied_threshold is None:
        return False
    return current_net_edge >= threshold.applied_threshold
