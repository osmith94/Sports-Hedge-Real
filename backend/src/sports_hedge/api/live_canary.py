"""Wave 40 live-canary operator endpoints. Disarm does not cancel venue orders."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from sports_hedge.config import get_settings
from sports_hedge.execution.live_canary import (
    arm_live_runtime,
    disarm_live_runtime,
    heartbeat_live_runtime,
    live_runtime_armed,
    public_canary_status,
)

router = APIRouter(prefix="/live-canary", tags=["live-canary"])

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


class ArmRequest(BaseModel):
    phrase: str = Field(min_length=1)


def _require_loopback(request: Request) -> None:
    client = request.client.host if request.client is not None else ""
    if client not in _LOCAL_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live canary arming is loopback-only.",
        )
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if origin and "127.0.0.1" not in origin and "localhost" not in origin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live canary arming rejects non-local origins.",
        )


@router.get("/status")
def live_canary_status() -> dict[str, Any]:
    """Read-only canary posture. Does not call venues or treat transport as permission."""

    return public_canary_status(get_settings())


@router.post("/arm")
async def arm_live_canary(body: ArmRequest, request: Request) -> dict[str, Any]:
    """Runtime ON after confirmation + read-only preflight. Boot remains OFF."""

    _require_loopback(request)
    return await arm_live_runtime(get_settings(), phrase=body.phrase)


@router.post("/disarm")
def disarm_live_canary(request: Request) -> dict[str, Any]:
    """Toggle OFF: halt new orders. Does not cancel existing venue orders."""

    _require_loopback(request)
    body = disarm_live_runtime(get_settings())
    body["halt_does_not_cancel_existing"] = True
    return body


@router.post("/heartbeat")
def heartbeat_live_canary(request: Request) -> dict[str, Any]:
    _require_loopback(request)
    if not heartbeat_live_runtime():
        return public_canary_status(get_settings())
    return public_canary_status(get_settings())


@router.post("/halt-new-orders")
def halt_new_live_canary_orders(request: Request) -> dict[str, Any]:
    """Alias for disarm. Does not cancel, replace, or claim existing orders cancelled."""

    return disarm_live_canary(request)


@router.get("/preflight")
async def live_canary_preflight(request: Request) -> dict[str, Any]:
    """Operator-local read-only probes. Not a submit path and not GET /health."""

    _require_loopback(request)
    from sports_hedge.execution.live_canary import collect_live_canary_facts, trading_permission_reasons

    settings = get_settings()
    facts = await collect_live_canary_facts(settings)
    reasons = trading_permission_reasons(settings, facts, for_arm=True)
    return {
        "trading_permission": not reasons,
        "not_ready_reasons": reasons,
        "runtime_armed": live_runtime_armed(),
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
