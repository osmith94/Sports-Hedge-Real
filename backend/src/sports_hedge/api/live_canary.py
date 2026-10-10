"""Wave 40 live-canary operator endpoints. Halt does not cancel venue orders."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from sports_hedge.config import get_settings
from sports_hedge.execution.live_canary import (
    get_live_canary_store,
    live_canary_enabled,
    public_canary_status,
)

router = APIRouter(prefix="/live-canary", tags=["live-canary"])


@router.get("/status")
def live_canary_status() -> dict[str, Any]:
    """Read-only canary posture. Does not call venues or treat transport as permission."""

    return public_canary_status(get_settings())


@router.get("/preflight")
async def live_canary_preflight() -> dict[str, Any]:
    """Operator-local read-only probes. Not a submit path and not GET /health."""

    from sports_hedge.execution.live_canary import collect_live_canary_facts, trading_permission_reasons

    settings = get_settings()
    facts = await collect_live_canary_facts(settings)
    reasons = trading_permission_reasons(settings, facts)
    return {
        "trading_permission": not reasons,
        "not_ready_reasons": reasons,
        "live_execution_ready_is_not_permission": True,
        "polymarket_geoblock_permitted": facts.polymarket_geoblock_permitted,
        "polymarket_geoblock_reason": facts.polymarket_geoblock_reason,
        "polymarket_credentials_present": facts.polymarket_credentials_present,
        "polymarket_wallet_valid": facts.polymarket_wallet_valid,
        "polymarket_balance_readable": facts.polymarket_balance_readable,
        "polymarket_trading_ready": facts.polymarket_trading_ready,
        "polymarket_trading_reason": facts.polymarket_trading_reason,
        "matchbook_credentials_present": facts.matchbook_credentials_present,
        "matchbook_authenticated": facts.matchbook_authenticated,
        "matchbook_free_funds_positive": facts.matchbook_free_funds_positive,
        "matchbook_currency_compatible": facts.matchbook_currency_compatible,
    }


@router.post("/halt-new-orders")
def halt_new_live_canary_orders() -> dict[str, Any]:
    """Durable HALT NEW ORDERS. Does not cancel, replace, or claim existing orders cancelled."""

    settings = get_settings()
    if not live_canary_enabled(settings):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Live canary is not enabled on this process.",
        )
    session_id = (settings.sports_hedge_live_canary_session_id or "").strip()
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Live canary session id is missing.",
        )
    get_live_canary_store(settings).halt_new_orders(
        session_id, reason="operator_halt_new_orders"
    )
    body = public_canary_status(settings)
    body["halt_does_not_cancel_existing"] = True
    return body
