"""Hand an accepted Price-2 plan to execute_live_package.

Fees, FX, edge, allocation, freshness, and depth are not recomputed. A
Matchbook unmatched remainder is cancelled with the existing offer cancel
when the offer id is already known. That is not a residual hedge.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sports_hedge.arbitrage.priority_alerts.models import LegExecutionMode
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.attempts import scrub_secrets
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
from sports_hedge.execution.package import (
    LIVE_EXECUTION_TRANSPORT_UNAVAILABLE,
    _accepted_plan,
    _frozen_package_ready,
    _outcome,
    _request_for_leg,
    execute_live_package,
)
from sports_hedge.execution.runtime import mark_dispatch_seam_available
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.trades import OPENING_TRANCHE_ID

mark_dispatch_seam_available()

_RESTING = frozenset({VenueOrderStatus.OPEN, VenueOrderStatus.PARTIAL})


class LiveDispatchResult:
    """One opening-package decision. ``sent`` is false when nothing was submitted."""

    def __init__(
        self,
        *,
        sent: bool,
        refusal: str | None = None,
        package: LiveExecutionPackage | None = None,
        requests: list[VenueOrderRequest] | None = None,
        remainder: str = "not_required",
    ) -> None:
        self.sent = sent
        self.refusal = refusal
        self.package = package
        self.requests = requests or []
        self.remainder = remainder


def opening_package_id(trade_id: str, tranche_id: str = OPENING_TRANCHE_ID) -> str:
    return f"{trade_id}:{tranche_id}"


def snapshot_ref(plan: PaperFillPlan) -> str | None:
    raw = plan.execution_snapshot_json
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("snapshot_id")
    return None if value is None else str(value)


def scrubbed_snapshot_json(plan: PaperFillPlan) -> str | None:
    raw = plan.execution_snapshot_json
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return json.dumps(scrub_secrets(payload), default=str)


def recovery_context(plan: PaperFillPlan, trade_id: str) -> str:
    """Leg identity stored with the attempt so a later restart can rebuild a trade.

    Economics are not copied. The client order id is the same digest dispatch uses.
    """

    legs: list[dict[str, Any]] = []
    for leg in plan.legs:
        if leg.requested_stake <= 0:
            continue
        request = _request_for_leg(
            leg,
            trade_id=trade_id,
            tranche_id=OPENING_TRANCHE_ID,
            snapshot_json=plan.execution_snapshot_json,
        )
        if request is None:
            continue
        mode = plan.execution_modes.get(leg.venue, LegExecutionMode.INTERNAL)
        legs.append(
            {
                "client_order_id": request.client_order_id,
                "venue": leg.venue.value,
                "outcome": leg.outcome,
                "currency": leg.currency,
                "requested_stake": str(leg.requested_stake),
                "displayed_odds": str(leg.displayed_odds),
                "source_market_id": leg.source_market_id,
                "source_runner_id": leg.source_runner_id,
                "source_event_id": leg.source_event_id,
                "execution_mode": mode.value,
            }
        )
    payload = {
        "canonical_event_id": plan.canonical_event_id,
        "canonical_market_id": plan.canonical_market_id,
        "scanned_at": plan.scanned_at.isoformat(),
        "provenance": plan.provenance.value if hasattr(plan.provenance, "value") else str(plan.provenance),
        "authority": {
            "settlement_equivalent": plan.settlement_equivalent,
            "execution_authoritative": plan.execution_authoritative,
            "eligible_for_paper_simulation": plan.eligible_for_paper_simulation,
            "solver_model": plan.decision.solver_model,
            "market_match": plan.decision.market_match.model_dump(mode="json"),
        },
        "legs": legs,
        "fx": [
            {
                "currency": item.currency,
                "gbp_per_unit": str(item.gbp_per_unit),
                "source": item.source,
                "captured_at": item.captured_at.isoformat(),
            }
            for item in plan.fx_snapshots
        ],
    }
    return json.dumps(scrub_secrets(payload), default=str)


def refusal_reason(
    plan: PaperFillPlan,
    *,
    settings: Settings,
    matchbook: MatchbookExecutionClient | None,
    kalshi: KalshiExecutionClient | None,
    polymarket: PolymarketExecutionClient | None = None,
) -> str | None:
    """Why this plan must not be submitted. None means the existing package may run."""

    if not (settings.sports_hedge_mode == "real" and settings.sports_hedge_execution_enabled is True):
        return "execution_disabled"
    if not _accepted_plan(plan):
        return "decision_not_accepted"
    legs = [leg for leg in plan.legs if leg.requested_stake > 0]
    if not legs:
        return "no_legs"
    requests = [
        _request_for_leg(
            leg,
            trade_id="pending",
            tranche_id="pending",
            snapshot_json=plan.execution_snapshot_json,
        )
        for leg in legs
    ]
    if any(request is None for request in requests):
        return "missing_native_ids"
    if not _frozen_package_ready(requests):
        return "frozen_execution_package_required"
    venues = {leg.venue for leg in legs}
    if not venues <= {VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET}:
        return "unsupported_venue"
    if VenueName.MATCHBOOK in venues and matchbook is None:
        return LIVE_EXECUTION_TRANSPORT_UNAVAILABLE
    if VenueName.KALSHI in venues and kalshi is None:
        return LIVE_EXECUTION_TRANSPORT_UNAVAILABLE
    if VenueName.POLYMARKET in venues and polymarket is None:
        return LIVE_EXECUTION_TRANSPORT_UNAVAILABLE
    return None


async def run_live_opening(
    plan: PaperFillPlan,
    *,
    trade_id: str,
    tranche_id: str,
    settings: Settings,
    matchbook: MatchbookExecutionClient | None,
    kalshi: KalshiExecutionClient | None,
    polymarket: PolymarketExecutionClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> LiveDispatchResult:
    """Submit once, then cancel a known Matchbook resting remainder."""

    reason = refusal_reason(
        plan,
        settings=settings,
        matchbook=matchbook,
        kalshi=kalshi,
        polymarket=polymarket,
    )
    if reason is not None:
        return LiveDispatchResult(sent=False, refusal=reason)
    legs = [leg for leg in plan.legs if leg.requested_stake > 0]
    requests = [
        request
        for request in (
            _request_for_leg(
                leg,
                trade_id=trade_id,
                tranche_id=tranche_id,
                snapshot_json=plan.execution_snapshot_json,
            )
            for leg in legs
        )
        if request is not None
    ]
    package = await execute_live_package(
        plan,
        trade_id=trade_id,
        tranche_id=tranche_id,
        settings=settings,
        matchbook=matchbook,
        kalshi=kalshi,
        polymarket=polymarket,
        clock=clock,
    )
    orders, remainder = await release_matchbook_remainders(
        package.orders,
        requests,
        matchbook=matchbook,
    )
    finished = package.model_copy(update={"orders": orders, "outcome": _outcome(orders)})
    return LiveDispatchResult(
        sent=True,
        package=finished,
        requests=requests,
        remainder=remainder,
    )


async def release_matchbook_remainders(
    orders: list[VenueOrderResult],
    requests: list[VenueOrderRequest],
    *,
    matchbook: MatchbookExecutionClient | None,
) -> tuple[list[VenueOrderResult], str]:
    """Cancel a known unmatched Matchbook remainder. Do not submit another order."""

    by_client = {request.client_order_id: request for request in requests}
    updated: list[VenueOrderResult] = []
    remainder = "not_required"
    for order in orders:
        resting = order.venue is VenueName.MATCHBOOK and order.status in _RESTING
        if not resting:
            updated.append(order)
            continue
        request = by_client.get(order.client_order_id)
        if request is None or matchbook is None or not order.venue_order_id:
            updated.append(order)
            remainder = "cancel_unproven"
            continue
        cancelled = await matchbook.cancel(request)
        if _cancel_replaced_fill(order, cancelled):
            updated.append(cancelled)
            if cancelled.status in _RESTING:
                remainder = "still_resting"
            else:
                remainder = "cancelled"
            continue
        updated.append(order)
        remainder = "still_resting"
    return updated, remainder


def _cancel_replaced_fill(original: VenueOrderResult, cancelled: VenueOrderResult) -> bool:
    """Keep the dispatch fill when cancel did not prove a replacement quantity."""

    if cancelled.status is VenueOrderStatus.FAILED and cancelled.filled_size is None:
        return False
    return cancelled.filled_size is not None or original.filled_size is None


def order_audit_facts(
    orders: list[VenueOrderResult],
    requests: list[VenueOrderRequest],
) -> list[dict[str, Any]]:
    """Execution facts for one package. Filled size null stays null."""

    by_client = {request.client_order_id: request for request in requests}
    facts: list[dict[str, Any]] = []
    for order in orders:
        request = by_client.get(order.client_order_id)
        facts.append(
            {
                "venue": order.venue.value,
                "native_event_id": None if request is None else request.native_event_id,
                "native_market_id": None if request is None else request.native_market_id,
                "native_runner_id": None if request is None else request.native_runner_id,
                "client_order_id": order.client_order_id,
                "submitted_at": order.submitted_at.isoformat(),
                "requested_price": str(order.requested_price),
                "requested_size": str(order.requested_size),
                "venue_order_id": order.venue_order_id,
                "venue_status": order.status.value,
                "filled_size": None if order.filled_size is None else str(order.filled_size),
                "average_fill_price": (
                    None if order.average_fill_price is None else str(order.average_fill_price)
                ),
                "updated_at": order.updated_at.isoformat(),
                "order_type": order.order_type,
                "venue_fee": None if order.venue_fee is None else str(order.venue_fee),
                "venue_fee_rate_bps": (
                    None if order.venue_fee_rate_bps is None else str(order.venue_fee_rate_bps)
                ),
                "remainder": (
                    None if order.remainder_quantity is None else str(order.remainder_quantity)
                ),
                "cancel_result": order.cancel_result,
                "eligibility": order.eligibility,
                "note": order.note,
            }
        )
    return facts


def package_outcome(package: LiveExecutionPackage | None) -> LivePackageOutcome | None:
    if package is None:
        return None
    return package.outcome


def order_for_leg(
    leg: Any,
    orders: list[VenueOrderResult],
    *,
    trade_id: str,
    tranche_id: str,
) -> VenueOrderResult | None:
    request = _request_for_leg(leg, trade_id=trade_id, tranche_id=tranche_id)
    if request is None:
        return None
    for order in orders:
        if order.client_order_id == request.client_order_id:
            return order
    return None


def run_blocking(coro: Any) -> Any:
    """Run one coroutine from a synchronous thread that does not own the scanner loop.

    When a loop is already running on this thread, ``thread.join()`` stalls that
    loop until the worker finishes. Price-2 item capture therefore runs this
    persist/dispatch function off the scanner loop via ``asyncio.to_thread``.
    Do not call this helper on the HOT/UNIVERSE event-loop thread.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — caller persists the reservation
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def now_utc() -> datetime:
    return datetime.now(UTC)
