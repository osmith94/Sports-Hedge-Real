"""Submit an already-approved Price-2 hedge without repeating Price-2.

The plan's exact native identifiers, prices, and sizes are the order. Fees,
FX, freshness, skew, depth, allocation, and Treasury are not recomputed here.
This function is not called by paper autofill.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from logging import getLogger
from typing import Any

from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.clients import (
    KalshiExecutionClient,
    MatchbookExecutionClient,
    PolymarketExecutionClient,
)
from sports_hedge.execution.models import (
    LiveExecutionPackage,
    LivePackageOutcome,
    VenueOrderRequest,
    VenueOrderResult,
    VenueOrderStatus,
)
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg

_SUPPORTED = frozenset({VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET})
LIVE_EXECUTION_TRANSPORT_UNAVAILABLE = "LIVE_EXECUTION_TRANSPORT_UNAVAILABLE"
LOGGER = getLogger("sports_hedge.execution.dispatch")


def log_execution_dispatch(phase: str, **fields: object) -> None:
    """One dispatch audit line. Economic facts are not recomputed here."""

    parts = [f"phase={phase}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    LOGGER.info(" ".join(parts))


def execution_capability(settings: Settings) -> dict[str, bool | str]:
    """Operator-facing execution posture. Configuration is not live-order capability.

    Credential presence is not a live order path. Scanner execution is live only
    when REAL mode, execution, armed transports, and the dispatch seam all hold.
    """

    from sports_hedge.execution import dispatch as _dispatch_seam
    from sports_hedge.execution.composition import bind_process_execution_runtime
    from sports_hedge.execution.runtime import scanner_execution_posture

    del _dispatch_seam
    bind_process_execution_runtime(settings)
    posture = scanner_execution_posture()
    armed = execution_armed(settings)
    ready = bool(armed and posture["live_execution_ready"])
    return {
        "configured_execution_enabled": settings.sports_hedge_execution_enabled is True,
        "matchbook_execution_configured": _matchbook_execution_configured(settings),
        "kalshi_execution_configured": _kalshi_execution_configured(settings),
        "polymarket_execution_configured": _polymarket_execution_configured(settings),
        "live_execution_ready": ready,
        "execution_transport": "armed" if ready else "unavailable",
        "scanner_execution": "live" if ready else "paper",
    }


def execution_armed(settings: Settings) -> bool:
    """True only when this repository is explicitly in real mode with execution on."""

    return settings.sports_hedge_mode == "real" and settings.sports_hedge_execution_enabled is True


def _matchbook_execution_configured(settings: Settings) -> bool:
    username = (settings.matchbook_username or "").strip()
    password = (settings.matchbook_password or "").strip()
    return bool(username and password)


def _kalshi_execution_configured(settings: Settings) -> bool:
    return bool(
        (settings.kalshi_api_key_id or "").strip()
        and (settings.kalshi_private_key_path or "").strip()
    )


def _polymarket_execution_configured(settings: Settings) -> bool:
    return bool((settings.polymarket_private_key_path or "").strip())


async def execute_live_package(
    plan: PaperFillPlan,
    *,
    trade_id: str,
    tranche_id: str,
    settings: Settings,
    matchbook: MatchbookExecutionClient | None = None,
    kalshi: KalshiExecutionClient | None = None,
    polymarket: PolymarketExecutionClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> LiveExecutionPackage:
    """Prepare hedge legs from the accepted plan and dispatch them concurrently."""

    now = clock or (lambda: datetime.now(UTC))
    if not execution_armed(settings):
        return LiveExecutionPackage(outcome=LivePackageOutcome.FAILED, detail="execution_disabled")
    if not _accepted_plan(plan):
        return LiveExecutionPackage(outcome=LivePackageOutcome.FAILED, detail="decision_not_accepted")
    prepared = [
        _request_for_leg(
            leg,
            trade_id=trade_id,
            tranche_id=tranche_id,
            snapshot_json=plan.execution_snapshot_json,
        )
        for leg in plan.legs
        if leg.requested_stake > 0
    ]
    if not prepared:
        return LiveExecutionPackage(outcome=LivePackageOutcome.FAILED, detail="no_legs")
    if _required_transport_missing(
        prepared, matchbook=matchbook, kalshi=kalshi, polymarket=polymarket
    ):
        return LiveExecutionPackage(
            outcome=LivePackageOutcome.FAILED,
            detail=LIVE_EXECUTION_TRANSPORT_UNAVAILABLE,
        )

    async def _one(request: VenueOrderRequest | None, leg: PaperOpportunityLeg) -> VenueOrderResult:
        submitted = now()
        if request is None or leg.venue not in _SUPPORTED:
            return _unsent(leg, trade_id=trade_id, tranche_id=tranche_id, at=submitted)
        client = _client_for(leg.venue, matchbook=matchbook, kalshi=kalshi, polymarket=polymarket)
        if client is None:
            return _unsent(leg, trade_id=trade_id, tranche_id=tranche_id, at=submitted)
        try:
            return await client.dispatch(request)
        except Exception:  # noqa: BLE001 — one leg's transport error must not cancel the others
            return _unsent(leg, trade_id=trade_id, tranche_id=tranche_id, at=now())

    legs = [leg for leg in plan.legs if leg.requested_stake > 0]
    log_execution_dispatch(
        "DISPATCH_BEGINS",
        snapshot_id=snapshot_ref_from_json(plan.execution_snapshot_json),
        legs=len(prepared),
    )
    log_execution_dispatch(
        "NO_PRE_SUBMIT_ECONOMIC_REFRESH",
        snapshot_id=snapshot_ref_from_json(plan.execution_snapshot_json),
    )
    orders = list(
        await asyncio.gather(
            *(_one(request, leg) for request, leg in zip(prepared, legs, strict=True))
        )
    )
    log_execution_dispatch(
        "ORDERS_SUBMITTED",
        snapshot_id=snapshot_ref_from_json(plan.execution_snapshot_json),
        orders=len(orders),
    )
    log_execution_dispatch(
        "VENUE_RESPONSES_PERSISTED",
        outcome=_outcome(orders).value,
    )
    return LiveExecutionPackage(outcome=_outcome(orders), orders=orders)


def _client_for(
    venue: VenueName,
    *,
    matchbook: MatchbookExecutionClient | None,
    kalshi: KalshiExecutionClient | None,
    polymarket: PolymarketExecutionClient | None,
) -> MatchbookExecutionClient | KalshiExecutionClient | PolymarketExecutionClient | None:
    if venue is VenueName.MATCHBOOK:
        return matchbook
    if venue is VenueName.KALSHI:
        return kalshi
    if venue is VenueName.POLYMARKET:
        return polymarket
    return None


def _required_transport_missing(
    prepared: list[VenueOrderRequest | None],
    *,
    matchbook: MatchbookExecutionClient | None,
    kalshi: KalshiExecutionClient | None,
    polymarket: PolymarketExecutionClient | None,
) -> bool:
    """A supported leg with no injected client has no runtime transport.

    The deterministic test transport is never substituted here.
    """

    venues = {request.venue for request in prepared if request is not None}
    if VenueName.MATCHBOOK in venues and matchbook is None:
        return True
    if VenueName.KALSHI in venues and kalshi is None:
        return True
    return VenueName.POLYMARKET in venues and polymarket is None


def _accepted_plan(plan: PaperFillPlan) -> bool:
    """The Price-2 record already accepted this hedge. Economics are not recomputed."""

    if not plan.execution_authoritative or not plan.execution_snapshot_json:
        return False
    try:
        payload = json.loads(plan.execution_snapshot_json)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("accepted") is True


def snapshot_ref_from_json(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("snapshot_id") is None:
        return None
    return str(payload["snapshot_id"])


def _request_for_leg(
    leg: PaperOpportunityLeg,
    *,
    trade_id: str,
    tranche_id: str,
    snapshot_json: str | None = None,
) -> VenueOrderRequest | None:
    event_id = (leg.source_event_id or "").strip()
    market_id = leg.source_market_id.strip()
    runner_id = leg.source_runner_id.strip()
    if not event_id or not market_id or not runner_id or leg.venue not in _SUPPORTED:
        return None
    frozen = _frozen_fields(snapshot_json, venue=leg.venue.value, runner_id=runner_id)
    return VenueOrderRequest(
        venue=leg.venue,
        trade_id=trade_id,
        tranche_id=tranche_id,
        native_event_id=event_id,
        native_market_id=market_id,
        native_runner_id=runner_id,
        side=MarketSide.BACK,
        currency=leg.currency,
        requested_price=leg.displayed_odds,
        requested_size=leg.requested_stake,
        client_order_id=_client_order_id(
            trade_id=trade_id,
            tranche_id=tranche_id,
            venue=leg.venue,
            event_id=event_id,
            market_id=market_id,
            runner_id=runner_id,
        ),
        **frozen,
    )


def _frozen_fields(raw: str | None, *, venue: str, runner_id: str) -> dict[str, Any]:
    """Copy one frozen native order. Missing JSON leaves the request unfrozen."""

    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    orders = payload.get("frozen_orders")
    if not isinstance(orders, list):
        return {}
    matched: dict[str, Any] | None = None
    for item in orders:
        if not isinstance(item, dict):
            continue
        if str(item.get("venue") or "") != venue:
            continue
        if str(item.get("native_runner_id") or "").strip() != runner_id:
            continue
        matched = item
        break
    if matched is None:
        return {"price2_snapshot_id": snapshot_ref_from_json(raw)}
    try:
        fields: dict[str, Any] = {"price2_snapshot_id": snapshot_ref_from_json(raw)}
        if venue == VenueName.POLYMARKET.value:
            fields.update(
                {
                    "frozen_order_type": matched.get("order_type"),
                    "frozen_limit_price": Decimal(str(matched["limit_price"])),
                    "frozen_amount": Decimal(str(matched["native_amount"])),
                    "frozen_shares": Decimal(str(matched["native_shares"])),
                    "frozen_tick_size": None if matched.get("tick_size") is None else str(matched["tick_size"]),
                    "frozen_minimum_size": Decimal(str(matched["minimum_order_size"])),
                }
            )
        elif venue == VenueName.MATCHBOOK.value:
            fields.update(
                {
                    "frozen_order_type": matched.get("native_side") or "back",
                    "frozen_limit_price": Decimal(str(matched["ladder_odds"])),
                    "frozen_amount": Decimal(str(matched["native_stake"])),
                }
            )
    except (InvalidOperation, KeyError, ValueError):
        return {"price2_snapshot_id": snapshot_ref_from_json(raw)}
    return fields


def _client_order_id(
    *,
    trade_id: str,
    tranche_id: str,
    venue: VenueName,
    event_id: str,
    market_id: str,
    runner_id: str,
) -> str:
    material = f"{trade_id}|{tranche_id}|{venue.value}|{event_id}|{market_id}|{runner_id}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"sh-{digest}"


def _unsent(
    leg: PaperOpportunityLeg,
    *,
    trade_id: str,
    tranche_id: str,
    at: datetime,
) -> VenueOrderResult:
    event_id = (leg.source_event_id or "").strip()
    return VenueOrderResult(
        venue=leg.venue,
        client_order_id=_client_order_id(
            trade_id=trade_id,
            tranche_id=tranche_id,
            venue=leg.venue,
            event_id=event_id,
            market_id=leg.source_market_id,
            runner_id=leg.source_runner_id,
        ),
        venue_order_id=None,
        status=VenueOrderStatus.FAILED,
        requested_size=leg.requested_stake,
        filled_size=Decimal(0),
        requested_price=leg.displayed_odds,
        average_fill_price=None,
        submitted_at=at,
        updated_at=at,
    )


def _outcome(orders: list[VenueOrderResult]) -> LivePackageOutcome:
    if not orders:
        return LivePackageOutcome.FAILED
    complete = all(
        order.status is VenueOrderStatus.FILLED and order.filled_size == order.requested_size
        for order in orders
    )
    if complete:
        return LivePackageOutcome.FULLY_FILLED
    if any(order.filled_size is not None and order.filled_size > 0 for order in orders):
        return LivePackageOutcome.PARTIAL
    return LivePackageOutcome.FAILED
