"""Validated Price-2 snapshot a future live arm can consume before any order.

The object is the execution-quality record of one hedge. It is built only from
the exact books just retrieved. It does not carry discovery prices, depth, or
stakes. ``accepted`` is the gate: a live layer must refuse to submit orders
when it is false. This module does not place venue orders.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sports_hedge.application.executable_liquidity import decision_net_edge

# Initial PAPER validation bound. Cross-venue and constituent books must be
# retrieved within this window of each other. It is intentionally tighter than
# the 2,000 ms quote-age gate: two individually fresh books can still be too
# far apart to be one execution snapshot. 500 ms covers ordinary concurrent
# provider latency without treating a multi-hundred-millisecond leg as coherent.
DEFAULT_MAX_SNAPSHOT_SKEW_MS = 500


@dataclass(frozen=True)
class ExecutionRetrieval:
    """One exact native book and the instant it was retrieved."""

    venue: str
    native_id: str
    event_id: str | None
    retrieved_at: datetime


@dataclass(frozen=True)
class ExecutionLegQuote:
    """Executable price and depth taken from the Price-2 scan of those books."""

    venue: str
    outcome: str
    native_market_id: str
    native_runner_id: str
    displayed_odds: str
    available_depth: str
    requested_stake: str
    levels: tuple[tuple[str, str], ...]
    quote_age_ms: int | None
    retrieved_at: datetime | None
    retrieval_native_id: str | None = None


@dataclass(frozen=True)
class ExecutionVenueCostEvidence:
    """Fee rule applied to one venue. Not the provider order book."""

    venue: str
    action: str
    fee_basis: str
    known_status: str
    source: str
    captured_at: str
    rate: str | None = None
    fixed_amount: str | None = None
    formula_name: str | None = None
    formula_parameters: tuple[tuple[str, str], ...] = ()
    currency: str = "GBP"
    effective_from: str | None = None
    snapshot_id: str | None = None
    source_market_id: str | None = None
    order_role: str = ""
    fee_scope: str = ""


@dataclass(frozen=True)
class ExecutionFeeEvidence:
    """Profit-haircut snapshot when that representation was applied."""

    venue: str
    source: str
    captured_at: str
    profit_haircut_rate: str
    zero_rate_basis: str | None = None


@dataclass(frozen=True)
class ExecutionFxEvidence:
    """One FX rate used to value a currency in the Price-2 economics."""

    currency: str
    gbp_per_unit: str
    source: str
    captured_at: str
    spread_bps: str | None = None
    conversion_slippage_bps: str | None = None


@dataclass(frozen=True)
class ExecutionCapitalEvidence:
    """Allocation constraint that sized the hedge. Balances are not copied."""

    limiting_constraint: str | None
    limiting_constraint_detail: str | None
    maximum_validated_capital: str | None
    recommended_committed_capital: str | None
    solver_model: str | None
    hard_constraints: tuple[str, ...] = ()
    paper_only: bool = True
    max_event_gbp: str | None = None
    event_deployed_gbp: str | None = None
    event_room_gbp: str | None = None
    max_opportunity_gbp: str | None = None
    opportunity_deployed_gbp: str | None = None
    opportunity_room_gbp: str | None = None
    max_one_time_gbp: str | None = None


@dataclass(frozen=True)
class ExecutionTimingCall:
    venue: str
    stage: str
    source_id: str
    outcome: str
    slot_wait_ms: int
    io_ms: int


@dataclass(frozen=True)
class ExecutionTiming:
    """Slot wait, I/O, and quote ages for this attempt. Not a provider payload."""

    assembly_ms: int
    calls: tuple[ExecutionTimingCall, ...] = ()
    quote_age_ms: tuple[tuple[str, int | None], ...] = ()


@dataclass
class ExecutionSnapshot:
    """One complete-set execution reprice, accepted only when skew and economics pass."""

    catalogue_row_id: str
    started_at: datetime
    retrievals: tuple[ExecutionRetrieval, ...]
    canonical_market_id: str | None = None
    evaluated_at: datetime | None = None
    legs: tuple[ExecutionLegQuote, ...] = ()
    oldest_quote_age_ms: int | None = None
    max_quote_age_ms: int = 2000
    max_skew_ms: int = DEFAULT_MAX_SNAPSHOT_SKEW_MS
    net_edge: str | None = None
    guaranteed_profit: str | None = None
    accepted: bool = False
    rejection_reason: str | None = None
    snapshot_id: str = ""
    venue_costs: tuple[ExecutionVenueCostEvidence, ...] = ()
    fee_snapshots: tuple[ExecutionFeeEvidence, ...] = ()
    fx_rates: tuple[ExecutionFxEvidence, ...] = ()
    minimum_net_edge: str | None = None
    minimum_net_edge_source: str | None = None
    minimum_net_edge_scope: str | None = None
    capital_constraint: ExecutionCapitalEvidence | None = None
    timing: ExecutionTiming | None = None
    execution_cycle: int = 0

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            self.snapshot_id = f"exec:{self.catalogue_row_id}:{uuid4()}"

    @property
    def earliest_retrieval_at(self) -> datetime | None:
        if not self.retrievals:
            return None
        return min(item.retrieved_at for item in self.retrievals)

    @property
    def latest_retrieval_at(self) -> datetime | None:
        if not self.retrievals:
            return None
        return max(item.retrieved_at for item in self.retrievals)

    @property
    def skew_ms(self) -> int | None:
        """latest_retrieval_at - earliest_retrieval_at, in milliseconds."""

        earliest = self.earliest_retrieval_at
        latest = self.latest_retrieval_at
        if earliest is None or latest is None:
            return None
        return max(0, int((latest - earliest).total_seconds() * 1000))

    def skew_exceeded(self) -> bool:
        skew = self.skew_ms
        if skew is None:
            return True
        return skew > self.max_skew_ms

    def audit_line(self) -> str:
        earliest = self.earliest_retrieval_at
        latest = self.latest_retrieval_at
        return " ".join(
            (
                f"snapshot_id={self.snapshot_id}",
                f"execution_cycle={self.execution_cycle}",
                f"skew_ms={self.skew_ms if self.skew_ms is not None else 'unknown'}",
                f"max_skew_ms={self.max_skew_ms}",
                f"earliest_retrieval_at={earliest.isoformat() if earliest else 'unknown'}",
                f"latest_retrieval_at={latest.isoformat() if latest else 'unknown'}",
                f"retrievals={len(self.retrievals)}",
                f"accepted={str(self.accepted).lower()}",
            )
        )

    def to_json(self) -> str:
        """Stable record of native ids, retrieval times, prices, and depth."""

        payload = {
            "snapshot_id": self.snapshot_id,
            "execution_cycle": self.execution_cycle,
            "catalogue_row_id": self.catalogue_row_id,
            "canonical_market_id": self.canonical_market_id,
            "started_at": self.started_at.isoformat(),
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "earliest_retrieval_at": (
                self.earliest_retrieval_at.isoformat() if self.earliest_retrieval_at else None
            ),
            "latest_retrieval_at": (
                self.latest_retrieval_at.isoformat() if self.latest_retrieval_at else None
            ),
            "skew_ms": self.skew_ms,
            "max_skew_ms": self.max_skew_ms,
            "oldest_quote_age_ms": self.oldest_quote_age_ms,
            "max_quote_age_ms": self.max_quote_age_ms,
            "net_edge": self.net_edge,
            "guaranteed_profit": self.guaranteed_profit,
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "minimum_net_edge": self.minimum_net_edge,
            "minimum_net_edge_source": self.minimum_net_edge_source,
            "minimum_net_edge_scope": self.minimum_net_edge_scope,
            "venue_costs": [_venue_cost_json(item) for item in self.venue_costs],
            "fee_snapshots": [_fee_json(item) for item in self.fee_snapshots],
            "fx_rates": [_fx_json(item) for item in self.fx_rates],
            "capital_constraint": _capital_json(self.capital_constraint),
            "timing": _timing_json(self.timing),
            "retrievals": [
                {
                    "venue": item.venue,
                    "native_id": item.native_id,
                    "event_id": item.event_id,
                    "retrieved_at": item.retrieved_at.isoformat(),
                }
                for item in self.retrievals
            ],
            "legs": [
                {
                    "venue": leg.venue,
                    "outcome": leg.outcome,
                    "native_market_id": leg.native_market_id,
                    "native_runner_id": leg.native_runner_id,
                    "displayed_odds": leg.displayed_odds,
                    "available_depth": leg.available_depth,
                    "requested_stake": leg.requested_stake,
                    "quote_age_ms": leg.quote_age_ms,
                    "retrieved_at": leg.retrieved_at.isoformat() if leg.retrieved_at else None,
                    "retrieval_native_id": leg.retrieval_native_id,
                    "levels": [{"odds": odds, "depth": depth} for odds, depth in leg.levels],
                }
                for leg in self.legs
            ],
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def execution_snapshot_id_from_json(raw: str | None) -> str | None:
    """Snapshot id stored on a fill plan. None when the plan has no Price-2 record."""

    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("snapshot_id")
    if not value:
        return None
    return str(value)


def _settings_int(settings: Any, name: str, default: int) -> int:
    """Read an int setting. Missing and None use the default; explicit 0 stays 0."""

    if settings is None or not hasattr(settings, name):
        return default
    value = getattr(settings, name)
    if value is None:
        return default
    return int(value)


def execution_max_snapshot_skew_ms(settings: Any) -> int:
    """Configured skew bound, never looser than the active quote-age gate.

    Explicit 0 is a real bound. Truthiness must not turn it back into 500 ms.
    A configured value tighter than the default is kept. The quote-age gate
    can only tighten the bound further, never loosen it.
    """

    configured = _settings_int(
        settings,
        "paper_execution_max_snapshot_skew_ms",
        DEFAULT_MAX_SNAPSHOT_SKEW_MS,
    )
    freshness = _settings_int(settings, "paper_entry_max_quote_age_ms", 2000)
    return max(0, min(configured, freshness))


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def retrieval_for_leg(
    leg: Any,
    retrievals: tuple[ExecutionRetrieval, ...] | list[ExecutionRetrieval],
) -> ExecutionRetrieval | None:
    """The exact native book for one fill leg.

    Matchbook legs share that market's retrieval. Kalshi and Polymarket legs
    match the ticker or token, including ``ticker:outcome`` runner ids. An
    ambiguous venue with no exact id match is left unresolved. The earliest
    timestamp for the venue is not a substitute.
    """

    venue = _enum_text(getattr(leg, "venue", ""))
    candidates = [item for item in retrievals if item.venue == venue]
    if not candidates:
        return None
    runner = str(getattr(leg, "source_runner_id", "") or "")
    market = str(getattr(leg, "source_market_id", "") or "")
    identities = {runner, market}
    for item in candidates:
        if item.native_id and item.native_id in identities:
            return item
    for item in candidates:
        native = item.native_id
        if not native:
            continue
        prefix = f"{native}:"
        if runner.startswith(prefix) or market.startswith(prefix):
            return item
    if len(candidates) == 1:
        return candidates[0]
    return None


def leg_quotes_from_decision(
    decision: Any,
    *,
    retrievals: tuple[ExecutionRetrieval, ...] | list[ExecutionRetrieval] = (),
    retrieved_at_by_venue: dict[str, datetime] | None = None,
) -> tuple[ExecutionLegQuote, ...]:
    """Prices, depth, and stakes from the Price-2 decision only.

    ``retrieved_at_by_venue`` remains for callers that have one book per venue.
    When retrievals are present, each leg uses its own native book.
    """

    quotes: list[ExecutionLegQuote] = []
    for leg in decision.fill_legs:
        levels = tuple(
            (str(level.decimal_odds), str(level.available_stake)) for level in (leg.levels or [])
        )
        depth = sum((level.available_stake for level in (leg.levels or [])), Decimal(0))
        venue = leg.venue.value
        matched = retrieval_for_leg(leg, retrievals) if retrievals else None
        retrieved_at = None if matched is None else matched.retrieved_at
        native_id = None if matched is None else matched.native_id
        if retrieved_at is None and retrieved_at_by_venue is not None and not retrievals:
            retrieved_at = retrieved_at_by_venue.get(venue)
        quotes.append(
            ExecutionLegQuote(
                venue=venue,
                outcome=str(leg.outcome),
                native_market_id=str(leg.source_market_id),
                native_runner_id=str(leg.source_runner_id),
                displayed_odds=str(leg.displayed_odds),
                available_depth=str(depth),
                requested_stake=str(leg.requested_stake),
                levels=levels,
                quote_age_ms=leg.quote_age_ms,
                retrieved_at=retrieved_at,
                retrieval_native_id=native_id,
            )
        )
    return tuple(quotes)


def provenance_from_decision(decision: Any) -> dict[str, Any]:
    """Compact fee, FX, trigger, and capital evidence from the Price-2 decision."""

    minimum = getattr(decision, "minimum_net_edge", None)
    scope = getattr(decision, "min_net_edge_scope", None)
    return {
        "venue_costs": tuple(
            _venue_cost_evidence(cost) for cost in (getattr(decision, "venue_costs", None) or [])
        ),
        "fee_snapshots": tuple(
            _fee_evidence(fee) for fee in (getattr(decision, "fee_snapshots", None) or [])
        ),
        "fx_rates": tuple(
            _fx_evidence(rate) for rate in (getattr(decision, "fx_snapshots", None) or [])
        ),
        "minimum_net_edge": _text(minimum),
        "minimum_net_edge_source": _text(getattr(decision, "min_net_edge_source", None)),
        "minimum_net_edge_scope": None if scope is None else _enum_text(scope),
        "capital_constraint": _capital_evidence(getattr(decision, "allocation", None)),
    }


def _venue_cost_evidence(cost: Any) -> ExecutionVenueCostEvidence:
    parameters = getattr(cost, "formula_parameters", None) or {}
    packed = tuple(sorted((str(key), str(value)) for key, value in parameters.items()))
    return ExecutionVenueCostEvidence(
        venue=_enum_text(cost.venue),
        action=_enum_text(cost.action),
        fee_basis=_enum_text(cost.fee_basis),
        known_status=_enum_text(cost.known_status),
        source=str(cost.source),
        rate=_text(cost.rate),
        fixed_amount=_text(cost.fixed_amount),
        formula_name=_text(cost.formula_name),
        formula_parameters=packed,
        currency=str(cost.currency),
        captured_at=cost.captured_at.isoformat(),
        effective_from=_iso(cost.effective_from),
        snapshot_id=_text(cost.snapshot_id),
        source_market_id=_text(cost.source_market_id),
        order_role=_enum_text(cost.order_role),
        fee_scope=_enum_text(cost.fee_scope),
    )


def _fee_evidence(fee: Any) -> ExecutionFeeEvidence:
    basis = getattr(fee, "zero_rate_basis", None)
    return ExecutionFeeEvidence(
        venue=_enum_text(fee.venue),
        source=str(fee.source),
        captured_at=fee.captured_at.isoformat(),
        profit_haircut_rate=str(fee.profit_haircut_rate),
        zero_rate_basis=None if basis is None else str(basis),
    )


def _fx_evidence(rate: Any) -> ExecutionFxEvidence:
    return ExecutionFxEvidence(
        currency=str(rate.currency),
        gbp_per_unit=str(rate.gbp_per_unit),
        source=str(rate.source),
        captured_at=rate.captured_at.isoformat(),
        spread_bps=_text(rate.spread_bps),
        conversion_slippage_bps=_text(rate.conversion_slippage_bps),
    )


def _capital_evidence(allocation: Any) -> ExecutionCapitalEvidence | None:
    if allocation is None:
        return None
    limiting = getattr(allocation, "limiting_constraint", None)
    constraints = tuple(
        _enum_text(item.kind) for item in (getattr(allocation, "hard_constraints", None) or [])
    )
    return ExecutionCapitalEvidence(
        limiting_constraint=None if limiting is None else _enum_text(limiting),
        limiting_constraint_detail=_text(getattr(allocation, "limiting_constraint_detail", None)),
        maximum_validated_capital=_text(getattr(allocation, "maximum_validated_capital", None)),
        recommended_committed_capital=_text(
            getattr(allocation, "recommended_committed_capital", None)
        ),
        solver_model=_text(getattr(allocation, "solver_model", None)),
        hard_constraints=constraints,
        paper_only=bool(getattr(allocation, "paper_only", True)),
        max_event_gbp=_text(getattr(allocation, "max_event_gbp", None)),
        event_deployed_gbp=_text(getattr(allocation, "event_deployed_gbp", None)),
        event_room_gbp=_text(getattr(allocation, "event_room_gbp", None)),
        max_opportunity_gbp=_text(getattr(allocation, "max_opportunity_gbp", None)),
        opportunity_deployed_gbp=_text(getattr(allocation, "opportunity_deployed_gbp", None)),
        opportunity_room_gbp=_text(getattr(allocation, "opportunity_room_gbp", None)),
        max_one_time_gbp=_text(getattr(allocation, "max_one_time_gbp", None)),
    )


def _venue_cost_json(item: ExecutionVenueCostEvidence) -> dict[str, Any]:
    return {
        "venue": item.venue,
        "action": item.action,
        "fee_basis": item.fee_basis,
        "known_status": item.known_status,
        "source": item.source,
        "rate": item.rate,
        "fixed_amount": item.fixed_amount,
        "formula_name": item.formula_name,
        "formula_parameters": [
            {"name": name, "value": value} for name, value in item.formula_parameters
        ],
        "currency": item.currency,
        "captured_at": item.captured_at,
        "effective_from": item.effective_from,
        "snapshot_id": item.snapshot_id,
        "source_market_id": item.source_market_id,
        "order_role": item.order_role,
        "fee_scope": item.fee_scope,
    }


def _fee_json(item: ExecutionFeeEvidence) -> dict[str, Any]:
    return {
        "venue": item.venue,
        "source": item.source,
        "captured_at": item.captured_at,
        "profit_haircut_rate": item.profit_haircut_rate,
        "zero_rate_basis": item.zero_rate_basis,
    }


def _fx_json(item: ExecutionFxEvidence) -> dict[str, Any]:
    return {
        "currency": item.currency,
        "gbp_per_unit": item.gbp_per_unit,
        "source": item.source,
        "captured_at": item.captured_at,
        "spread_bps": item.spread_bps,
        "conversion_slippage_bps": item.conversion_slippage_bps,
    }


def _capital_json(item: ExecutionCapitalEvidence | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "limiting_constraint": item.limiting_constraint,
        "limiting_constraint_detail": item.limiting_constraint_detail,
        "maximum_validated_capital": item.maximum_validated_capital,
        "recommended_committed_capital": item.recommended_committed_capital,
        "solver_model": item.solver_model,
        "hard_constraints": list(item.hard_constraints),
        "paper_only": item.paper_only,
        "max_event_gbp": item.max_event_gbp,
        "event_deployed_gbp": item.event_deployed_gbp,
        "event_room_gbp": item.event_room_gbp,
        "max_opportunity_gbp": item.max_opportunity_gbp,
        "opportunity_deployed_gbp": item.opportunity_deployed_gbp,
        "opportunity_room_gbp": item.opportunity_room_gbp,
        "max_one_time_gbp": item.max_one_time_gbp,
    }


def _timing_json(item: ExecutionTiming | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "assembly_ms": item.assembly_ms,
        "quote_age_ms": {venue: age for venue, age in item.quote_age_ms},
        "calls": [
            {
                "venue": call.venue,
                "stage": call.stage,
                "source_id": call.source_id,
                "outcome": call.outcome,
                "slot_wait_ms": call.slot_wait_ms,
                "io_ms": call.io_ms,
            }
            for call in item.calls
        ],
    }


def snapshot_economics(decision: Any) -> tuple[str | None, str | None]:
    edge = decision_net_edge(decision)
    profit = _guaranteed_profit(decision)
    return (None if edge is None else str(edge), None if profit is None else str(profit))


def _guaranteed_profit(decision: Any) -> Decimal | None:
    allocation = decision.allocation
    if allocation is not None and allocation.accepted and allocation.guaranteed_profit > 0:
        return allocation.guaranteed_profit
    depth = decision.depth_scan
    if depth is not None and depth.solution.is_arbitrage:
        return depth.solution.guaranteed_profit
    payoff = decision.payoff_scan
    if payoff is not None and payoff.solution.is_arbitrage:
        return payoff.solution.minimum_state_pnl
    return None
