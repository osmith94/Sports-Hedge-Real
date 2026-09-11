from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus

NEAR_STATUSES = {OpportunityStatus.WATCHING, OpportunityStatus.APPROACHING}
TRIGGERED_STATUSES = {OpportunityStatus.TRIGGERED}


def rank_near_opportunities(
    opportunities: list[NearOpportunity],
    *,
    limit: int,
) -> list[NearOpportunity]:
    """Deterministic top-N ranking for below-trigger watch candidates.

    Closeness to trigger is primary, then fresher quotes, then deeper limiting
    GBP depth. Raw fields remain on each opportunity; this does not collapse them
    into a hidden score.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    eligible = [item for item in opportunities if item.status in NEAR_STATUSES]
    ordered = sorted(eligible, key=_near_rank_key)
    return ordered[:limit]


def rank_triggered_opportunities(
    opportunities: list[NearOpportunity],
    *,
    limit: int,
) -> list[NearOpportunity]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    eligible = [item for item in opportunities if item.status in TRIGGERED_STATUSES]
    ordered = sorted(eligible, key=_triggered_rank_key)
    return ordered[:limit]


def _near_rank_key(item: NearOpportunity) -> tuple:
    distance = (
        item.distance_to_trigger_pp if item.distance_to_trigger_pp is not None else Decimal("999")
    )
    depth = item.limiting_depth_gbp if item.limiting_depth_gbp is not None else Decimal("0")
    status_rank = 0 if item.status == OpportunityStatus.APPROACHING else 1
    return (
        distance,
        item.quote_age_ms,
        -depth,
        status_rank,
        item.canonical_market_id,
        item.opportunity_id,
    )


def _triggered_rank_key(item: NearOpportunity) -> tuple:
    edge = item.current_net_edge if item.current_net_edge is not None else Decimal("-1")
    depth = item.limiting_depth_gbp if item.limiting_depth_gbp is not None else Decimal("0")
    return (
        -edge,
        item.quote_age_ms,
        -depth,
        item.canonical_market_id,
        item.opportunity_id,
    )
