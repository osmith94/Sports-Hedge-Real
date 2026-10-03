"""Classify live attempts that have no persisted live trade.

This does not submit orders and does not hedge a residual. Unknown fill
quantities stay unknown. A reconstructed trade is recorded only by the caller
when every persisted fill is present and agrees with the recovery context.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

_REPORTS: list[dict[str, Any]] = []
_RECONSTRUCTED: dict[str, dict[str, Any]] = {}


def publish_orphaned_live_executions(rows: list[dict[str, Any]]) -> None:
    global _REPORTS
    _REPORTS = list(rows)


def orphaned_live_executions() -> list[dict[str, Any]]:
    return list(_REPORTS)


def remember_reconstructed(report: dict[str, Any]) -> None:
    package_id = str(report.get("package_id") or "")
    if package_id:
        _RECONSTRUCTED[package_id] = report


def reconstructed_report(package_id: str) -> dict[str, Any] | None:
    found = _RECONSTRUCTED.get(package_id)
    return None if found is None else dict(found)


def clear_orphan_reports() -> None:
    global _REPORTS
    _REPORTS = []
    _RECONSTRUCTED.clear()


def parse_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def parse_orders(raw: str | None) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        return None
    return payload


def fills_are_known(orders: list[dict[str, Any]]) -> bool:
    if not orders:
        return False
    for order in orders:
        if order.get("filled_size") is None:
            return False
        try:
            Decimal(str(order["filled_size"]))
            Decimal(str(order["requested_size"]))
        except (InvalidOperation, ValueError, KeyError):
            return False
    return True


def derived_outcome(orders: list[dict[str, Any]]) -> str | None:
    """FULLY_FILLED, PARTIAL, or FAILED from persisted sizes. None if unknown."""

    if not fills_are_known(orders):
        return None
    positive = False
    complete = True
    for order in orders:
        filled = Decimal(str(order["filled_size"]))
        requested = Decimal(str(order["requested_size"]))
        if filled > 0:
            positive = True
        if str(order.get("venue_status") or "").lower() != "filled" or filled != requested:
            complete = False
    if complete and positive:
        return "FULLY_FILLED"
    if positive:
        return "PARTIAL"
    return "FAILED"


def _same_text(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    text_left = str(left).strip()
    text_right = str(right).strip()
    return bool(text_left) and text_left == text_right


def _same_decimal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError, TypeError):
        return False


def orders_match_recovery(orders: list[dict[str, Any]], recovery: dict[str, Any]) -> bool:
    """Persisted order facts must match the stored leg identity field for field."""

    legs = recovery.get("legs")
    if not isinstance(legs, list) or not legs or len(legs) != len(orders):
        return False
    by_client: dict[str, dict[str, Any]] = {}
    for order in orders:
        client_order_id = str(order.get("client_order_id") or "")
        if not client_order_id or client_order_id in by_client:
            return False
        by_client[client_order_id] = order
    for leg in legs:
        if not isinstance(leg, dict):
            return False
        order = by_client.get(str(leg.get("client_order_id") or ""))
        if order is None:
            return False
        if not _same_text(order.get("venue"), leg.get("venue")):
            return False
        if not _same_text(order.get("native_event_id"), leg.get("source_event_id")):
            return False
        if not _same_text(order.get("native_market_id"), leg.get("source_market_id")):
            return False
        if not _same_text(order.get("native_runner_id"), leg.get("source_runner_id")):
            return False
        if not _same_decimal(order.get("requested_price"), leg.get("displayed_odds")):
            return False
        if not _same_decimal(order.get("requested_size"), leg.get("requested_stake")):
            return False
    return True
