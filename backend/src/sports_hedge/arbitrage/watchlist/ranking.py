from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus

NEAR_STATUSES = {OpportunityStatus.WATCHING, OpportunityStatus.APPROACHING}
TRIGGERED_STATUSES = {OpportunityStatus.TRIGGERED}
TRACKED_ACTIONABLE_STATUSES = {
    OpportunityStatus.TRIGGERED,
    OpportunityStatus.PAPER_FILLING,
    OpportunityStatus.PARTIAL,
}
TRACKED_EXCLUDED = {
    OpportunityStatus.CLOSED,
    OpportunityStatus.EXPIRED,
    OpportunityStatus.FILLED,
}


def opportunity_id_for_canonical_market(canonical_market_id: str) -> str:
    return f"watch:{canonical_market_id}"


def tracked_cohort_opportunity_ids(canonical_market_ids: Iterable[str | None]) -> set[str]:
    """Stable watchlist identities for one completed collection's paper decisions.

    Identity remains `watch:{canonical_market_id}`. Distinct lines, periods,
    settlement keys and venue-pair markets keep distinct canonical market ids.
    Repeated decisions for the same market collapse to one tracked row.
    """

    return {
        opportunity_id_for_canonical_market(market_id)
        for market_id in canonical_market_ids
        if market_id
    }


def filter_tracked_to_cohort(
    opportunities: Sequence[NearOpportunity],
    cohort_ids: Iterable[str],
) -> list[NearOpportunity]:
    wanted = set(cohort_ids)
    return [item for item in opportunities if item.opportunity_id in wanted]


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


def rank_tracked_opportunities(
    opportunities: list[NearOpportunity],
    *,
    limit: int,
) -> list[NearOpportunity]:
    """Operator board of the current radar merge.

    Explicit status/economics order, not a hidden composite score:
    actionable/TRIGGERED first by strongest current net margin, then
    approaching/watching by smallest distance to trigger, then rejected
    diagnostics. Negative net edges stay visible and are not reclassified.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    eligible = [item for item in opportunities if item.status not in TRACKED_EXCLUDED]
    ordered = sorted(eligible, key=_tracked_rank_key)
    return ordered[:limit]


def _near_rank_key(item: NearOpportunity) -> tuple:
    distance = (
        item.distance_to_trigger_pp if item.distance_to_trigger_pp is not None else Decimal("999")
    )
    depth = item.limiting_depth_gbp if item.limiting_depth_gbp is not None else Decimal("0")
    status_rank = 0 if item.status == OpportunityStatus.APPROACHING else 1
    age = item.quote_age_ms if item.quote_age_ms is not None else 10**12
    return (
        distance,
        age,
        -depth,
        status_rank,
        item.canonical_market_id,
        item.opportunity_id,
    )


def _triggered_rank_key(item: NearOpportunity) -> tuple:
    edge = item.current_net_edge if item.current_net_edge is not None else Decimal("-1")
    depth = item.limiting_depth_gbp if item.limiting_depth_gbp is not None else Decimal("0")
    age = item.quote_age_ms if item.quote_age_ms is not None else 10**12
    return (
        -edge,
        age,
        -depth,
        item.canonical_market_id,
        item.opportunity_id,
    )


def _tracked_rank_key(item: NearOpportunity) -> tuple:
    if item.status in TRACKED_ACTIONABLE_STATUSES:
        missing = item.current_net_edge is None
        edge = item.current_net_edge if item.current_net_edge is not None else Decimal("0")
        return (
            0,
            int(missing),
            -edge,
            item.canonical_market_id,
            item.opportunity_id,
        )
    if item.status in NEAR_STATUSES:
        missing = item.distance_to_trigger_pp is None
        distance = (
            item.distance_to_trigger_pp if item.distance_to_trigger_pp is not None else Decimal("0")
        )
        return (
            1,
            int(missing),
            distance,
            item.canonical_market_id,
            item.opportunity_id,
        )
    return (
        2,
        0,
        Decimal("0"),
        item.canonical_market_id,
        item.opportunity_id,
    )
