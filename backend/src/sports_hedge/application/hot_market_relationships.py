"""HOT quote-refresh identity over UNIVERSE-proved comparable rows.

UNIVERSE discovers fixtures, normalizes markets, proves MATCHED_EQUIVALENT or
PAPER_ASSUMED_EQUIVALENT (1X2 paper-mode only), and persists the exact venue
market relationship. HOT reads that identity and refreshes only current
quote/depth/status for those exact markets.

Relationship identity is source/canonical contract identity, never stale prices.
Kalshi fee snapshots cached here are fee metadata (type/multiplier/provenance),
not quotes. HOT must not call get_series merely to re-prove settlement.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from pydantic import BaseModel, Field

from sports_hedge.application.current_market_inventory import (
    CurrentMarketSlot,
    canonical_current_market_key,
    comparable_venue_pairs,
    slot_relationship_current,
)
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    VenueMarketFacts,
    inventory_is_hot_refreshable,
)
from sports_hedge.application.scan_lanes import TERMINAL_STATUSES
from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.domain.models import VenueName

HOT_RELATIONSHIP_MISSING_REASON = "hot_relationship_missing"
HOT_REVALIDATION_NEEDED_REASON = "hot_revalidation_needed"
HOT_RELATIONSHIP_UNAVAILABLE_REASON = "hot_relationship_unavailable"
PROOF_MATCHED_EQUIVALENT = InventoryComparisonStatus.MATCHED_EQUIVALENT.value


class HotVenueLeg(BaseModel):
    """Exact venue contract identity needed to refresh one ApprovedEquivalent leg."""

    venue: VenueName
    source_event_id: str
    source_market_id: str
    source_runner_ids: list[str] = Field(default_factory=list)
    constituent_contract_ids: list[str] = Field(default_factory=list)
    family: str | None = None
    period: str | None = None
    line: Decimal | None = None
    settlement_key: str | None = None
    canonical_identity: dict[str, Any] | None = None
    fee_snapshot: dict[str, Any] | None = None


class HotMarketRelationship(BaseModel):
    """One UNIVERSE-proved ApprovedEquivalent that HOT may quote-refresh."""

    canonical_event_id: str
    market_key: str
    family: str | None = None
    period: str | None = None
    line: Decimal | None = None
    settlement_key: str | None = None
    proof_status: str = PROOF_MATCHED_EQUIVALENT
    venue_pair: list[str] = Field(default_factory=list)
    matchbook: HotVenueLeg | None = None
    kalshi: HotVenueLeg | None = None
    polymarket: HotVenueLeg | None = None


def relationships_from_fixture_markets(
    fixture_markets: Mapping[str, list[FixtureMarketInventoryRow]],
) -> dict[str, list[HotMarketRelationship]]:
    """Build HOT-refresh identities from inventory rows. Prices are not identity."""

    payload: dict[str, list[HotMarketRelationship]] = {}
    for canonical_id, rows in fixture_markets.items():
        items = [
            relationship
            for row in rows
            if (relationship := relationship_from_inventory_row(canonical_id, row)) is not None
        ]
        if items:
            payload[str(canonical_id)] = items
    return payload


def relationships_from_current_slots(
    canonical_event_id: str,
    slots: list[CurrentMarketSlot],
    *,
    now: datetime,
    **kwargs: Any,
) -> list[HotMarketRelationship]:
    """Current, non-absent MATCHED_EQUIVALENT relationships for one HOT fixture."""

    items: list[HotMarketRelationship] = []
    for slot in slots:
        if slot.evaluated_absent:
            continue
        if not slot_relationship_current(slot, now=now, **kwargs):
            continue
        relationship = relationship_from_inventory_row(canonical_event_id, slot.row)
        if relationship is None:
            continue
        items.append(relationship)
    return items


def relationship_from_inventory_row(
    canonical_event_id: str,
    row: FixtureMarketInventoryRow,
) -> HotMarketRelationship | None:
    if not inventory_is_hot_refreshable(row.comparison_status):
        return None
    matchbook = _leg_from_facts(row.matchbook)
    kalshi = _leg_from_facts(row.kalshi)
    polymarket = _leg_from_facts(row.polymarket)
    legs = [item for item in (matchbook, kalshi, polymarket) if item is not None]
    if len(legs) < 2:
        return None
    pairs = sorted({f"{left}|{right}" for left, right in comparable_venue_pairs(row)})
    if not pairs:
        venues = sorted(item.venue.value for item in legs)
        pairs = [f"{left}|{right}" for index, left in enumerate(venues) for right in venues[index + 1 :]]
    settlement = _row_settlement_key(row)
    return HotMarketRelationship(
        canonical_event_id=canonical_event_id,
        market_key=canonical_current_market_key(row),
        family=row.family,
        period=row.period,
        line=row.line,
        settlement_key=settlement,
        proof_status=row.comparison_status.value,
        venue_pair=pairs,
        matchbook=matchbook,
        kalshi=kalshi,
        polymarket=polymarket,
    )


def index_hot_relationships(
    payload: Mapping[str, list[HotMarketRelationship | dict[str, Any]]] | None,
) -> tuple[dict[str, list[HotMarketRelationship]], dict[str, list[HotMarketRelationship]]]:
    """Index by canonical event id and venue:source_event_id for cluster lookup."""

    by_canonical: dict[str, list[HotMarketRelationship]] = {}
    by_source: dict[str, list[HotMarketRelationship]] = {}
    if not payload:
        return by_canonical, by_source
    for canonical_id, rows in payload.items():
        items: list[HotMarketRelationship] = []
        for row in rows:
            relationship = (
                row
                if isinstance(row, HotMarketRelationship)
                else HotMarketRelationship.model_validate(row)
            )
            items.append(relationship)
            for leg in (relationship.matchbook, relationship.kalshi, relationship.polymarket):
                if leg is None:
                    continue
                key = f"{leg.venue.value}:{leg.source_event_id}"
                by_source.setdefault(key, []).append(relationship)
        if items:
            by_canonical[str(canonical_id)] = items
    return by_canonical, by_source


def canonical_market_from_leg(leg: HotVenueLeg | None) -> CanonicalMarket | None:
    if leg is None or not isinstance(leg.canonical_identity, dict) or not leg.canonical_identity:
        return None
    try:
        return CanonicalMarket.model_validate(leg.canonical_identity)
    except Exception:
        return None


def matchbook_payload_is_terminal(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("state") or "").strip().casefold()
    return status in TERMINAL_STATUSES


def extract_matchbook_market_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    market = payload.get("market")
    if isinstance(market, dict) and _looks_like_matchbook_market(market):
        return market
    markets = payload.get("markets")
    if isinstance(markets, list):
        for item in markets:
            if isinstance(item, dict) and _looks_like_matchbook_market(item):
                return item
    if _looks_like_matchbook_market(payload):
        return dict(payload)
    return None


def hot_identity_matches_market(
    *,
    family: str | None,
    period: str | None,
    line: Decimal | None,
    settlement_key: str | None,
    source_market_id: str,
    expected: HotVenueLeg | HotMarketRelationship,
) -> bool:
    expected_market_id = (
        expected.source_market_id
        if isinstance(expected, HotVenueLeg)
        else _relationship_source_market_id(expected)
    )
    if expected_market_id and str(source_market_id) != str(expected_market_id):
        return False
    expected_family = expected.family if isinstance(expected, HotVenueLeg) else expected.family
    expected_period = expected.period if isinstance(expected, HotVenueLeg) else expected.period
    expected_line = expected.line if isinstance(expected, HotVenueLeg) else expected.line
    expected_settlement = (
        expected.settlement_key if isinstance(expected, HotVenueLeg) else expected.settlement_key
    )
    if expected_family and family and expected_family != family:
        return False
    if expected_period and period and expected_period != period:
        return False
    if not _same_line(expected_line, line):
        return False
    if expected_settlement and settlement_key and expected_settlement != settlement_key:
        return False
    return True


def kalshi_tickers_for_leg(leg: HotVenueLeg) -> list[str]:
    tickers = [item for item in leg.constituent_contract_ids if str(item).strip()]
    if tickers:
        return list(dict.fromkeys(tickers))
    derived: list[str] = []
    for runner_id in leg.source_runner_ids:
        ticker = str(runner_id).rsplit(":", 1)[0].strip()
        if ticker and ticker not in derived:
            derived.append(ticker)
    if derived:
        return derived
    market_id = str(leg.source_market_id or "").strip()
    return [market_id] if market_id else []


def fail_closed_inventory_row(
    relationship: HotMarketRelationship,
    *,
    reason: str,
) -> FixtureMarketInventoryRow:
    """Keep source identity, drop quotes, and mark the proved pair unavailable."""

    return FixtureMarketInventoryRow(
        display_name=relationship.family or relationship.market_key,
        family=relationship.family,
        period=relationship.period,
        line=relationship.line,
        comparison_status=InventoryComparisonStatus.OTHER,
        reason=reason,
        rejection_reasons=[reason],
        matchbook=_facts_from_leg(relationship.matchbook),
        kalshi=_facts_from_leg(relationship.kalshi),
        polymarket=_facts_from_leg(relationship.polymarket),
    )


def _facts_from_leg(leg: HotVenueLeg | None) -> VenueMarketFacts | None:
    if leg is None:
        return None
    return VenueMarketFacts(
        venue=leg.venue,
        source_event_id=leg.source_event_id,
        source_market_id=leg.source_market_id,
        family=leg.family,
        period=leg.period,
        line=leg.line,
        settlement_key=leg.settlement_key,
        source_runner_ids=list(leg.source_runner_ids),
        constituent_contract_ids=list(leg.constituent_contract_ids),
        canonical_identity=leg.canonical_identity,
        fee_snapshot=leg.fee_snapshot,
    )


def _leg_from_facts(facts: VenueMarketFacts | None) -> HotVenueLeg | None:
    if facts is None:
        return None
    source_event_id = str(facts.source_event_id or "").strip()
    source_market_id = str(facts.source_market_id or "").strip()
    if not source_event_id or not source_market_id:
        return None
    runners = [str(item).strip() for item in facts.source_runner_ids if str(item).strip()]
    contracts = [
        str(item).strip() for item in facts.constituent_contract_ids if str(item).strip()
    ]
    if not contracts and facts.venue is VenueName.KALSHI:
        for runner_id in runners:
            ticker = runner_id.rsplit(":", 1)[0].strip()
            if ticker and ticker not in contracts:
                contracts.append(ticker)
    identity = facts.canonical_identity if isinstance(facts.canonical_identity, dict) else None
    fee = facts.fee_snapshot if isinstance(facts.fee_snapshot, dict) else None
    return HotVenueLeg(
        venue=facts.venue,
        source_event_id=source_event_id,
        source_market_id=source_market_id,
        source_runner_ids=runners,
        constituent_contract_ids=contracts,
        family=facts.family,
        period=facts.period,
        line=facts.line,
        settlement_key=facts.settlement_key,
        canonical_identity=identity,
        fee_snapshot=fee,
    )


def _row_settlement_key(row: FixtureMarketInventoryRow) -> str | None:
    for facts in (row.matchbook, row.polymarket, row.kalshi):
        if facts is not None and facts.settlement_key:
            return facts.settlement_key
    return None


def _relationship_source_market_id(relationship: HotMarketRelationship) -> str | None:
    for leg in (relationship.matchbook, relationship.kalshi, relationship.polymarket):
        if leg is not None and leg.source_market_id:
            return leg.source_market_id
    return None


def _looks_like_matchbook_market(payload: Mapping[str, Any]) -> bool:
    if payload.get("id") is None and payload.get("market-id") is None:
        return False
    return "runners" in payload or "name" in payload or "status" in payload or "state" in payload


def _same_line(left: Decimal | str | None, right: Decimal | str | None) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError, TypeError):
        return str(left) == str(right)
