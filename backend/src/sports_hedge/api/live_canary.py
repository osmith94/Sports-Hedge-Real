"""Wave 40 live-canary operator endpoints. Disarm does not cancel venue orders."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

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

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testclient"})
_REQUEST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testclient", "testserver"})
_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ORIGIN_SCHEMES = frozenset({"http"})
_ORIGIN_PORTS = frozenset({None, 80, 3000, 3001, 8000})


class ArmRequest(BaseModel):
    phrase: str = Field(min_length=1)


def _hostname(value: str | None) -> str:
    host = (value or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _port_allowed(port: int | None) -> bool:
    return port in _ORIGIN_PORTS


def _origin_allowed(raw: str) -> bool:
    parsed = urlparse(raw.strip())
    if parsed.scheme not in _ORIGIN_SCHEMES:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path not in {"", "/"}:
        return False
    host = _hostname(parsed.hostname)
    if host not in _ORIGIN_HOSTS:
        return False
    return _port_allowed(parsed.port)


def _host_header_allowed(raw: str) -> bool:
    header = raw.strip()
    if not header:
        return False
    parsed = urlparse(f"http://{header}")
    host = _hostname(parsed.hostname)
    if host not in _REQUEST_HOSTS:
        return False
    if host in {"testclient", "testserver"}:
        return True
    return _port_allowed(parsed.port) or parsed.port in {None, 80, 8000}


def _require_loopback(request: Request, *, mutating: bool) -> None:
    client = request.client.host if request.client is not None else ""
    if client not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live canary arming is loopback-only.",
        )
    host = request.headers.get("host") or ""
    if not _host_header_allowed(host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live canary arming rejects non-local Host.",
        )
    origin = (request.headers.get("origin") or "").strip()
    referer = (request.headers.get("referer") or "").strip()
    if mutating:
        token = origin or referer
        if not token or not _origin_allowed(token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Live canary arming requires a parsed local Origin.",
            )
        return
    if origin and not _origin_allowed(origin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live canary arming rejects non-local origins.",
        )
    if referer and not origin and not _origin_allowed(referer):
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

    _require_loopback(request, mutating=True)
    return await arm_live_runtime(get_settings(), phrase=body.phrase)


@router.post("/disarm")
def disarm_live_canary(request: Request) -> dict[str, Any]:
    """Toggle OFF: halt new orders. Does not cancel existing venue orders."""

    _require_loopback(request, mutating=True)
    body = disarm_live_runtime(get_settings())
    body["halt_does_not_cancel_existing"] = True
    return body


@router.post("/heartbeat")
def heartbeat_live_canary(request: Request) -> dict[str, Any]:
    _require_loopback(request, mutating=True)
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

    _require_loopback(request, mutating=False)
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
