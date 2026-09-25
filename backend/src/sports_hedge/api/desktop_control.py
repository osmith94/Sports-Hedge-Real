"""Desktop-controller shutdown control for the paper API.

Only ``sports_hedge.api.desktop_host`` mounts this router. The normal
``uvicorn sports_hedge.api.main:app`` entry point has no shutdown route.

A shutdown request must satisfy every check:

- desktop mode is enabled for this process (``SPORTS_HEDGE_DESKTOP_MODE=1``);
- a per-launch secret of at least ``MIN_TOKEN_LENGTH`` characters was supplied
  by SportsHedge.exe through the process environment;
- the TCP peer is a loopback address;
- ``Authorization: Bearer <secret>`` matches (constant-time comparison).

The secret is never returned, logged or exposed to the browser. Accepting a
request only asks uvicorn to exit (``server.should_exit = True``), so the normal
FastAPI lifespan cleanup still runs.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request

LOGGER = logging.getLogger(__name__)

DESKTOP_MODE_ENV = "SPORTS_HEDGE_DESKTOP_MODE"
DESKTOP_SHUTDOWN_TOKEN_ENV = "SPORTS_HEDGE_DESKTOP_SHUTDOWN_TOKEN"
DESKTOP_SESSION_ID_ENV = "SPORTS_HEDGE_DESKTOP_SESSION_ID"
MIN_TOKEN_LENGTH = 32
SHUTDOWN_PATH = "/desktop/shutdown"
SESSION_PATH = "/desktop/session"


@dataclass(frozen=True)
class DesktopShutdownConfig:
    enabled: bool
    token: str | None = field(default=None, repr=False)
    session_id: str | None = None

    @classmethod
    def disabled(cls) -> DesktopShutdownConfig:
        return cls(enabled=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DesktopShutdownConfig:
        mode = (env.get(DESKTOP_MODE_ENV) or "").strip()
        token = (env.get(DESKTOP_SHUTDOWN_TOKEN_ENV) or "").strip()
        session_id = (env.get(DESKTOP_SESSION_ID_ENV) or "").strip() or None
        if mode != "1" or len(token) < MIN_TOKEN_LENGTH:
            return cls(enabled=False, session_id=session_id)
        return cls(enabled=True, token=token, session_id=session_id)


@dataclass(frozen=True)
class ShutdownDecision:
    allowed: bool
    status_code: int
    reason: str


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def authorize_desktop_shutdown(
    config: DesktopShutdownConfig,
    authorization: str | None,
    client_host: str | None,
) -> ShutdownDecision:
    """Pure authorization decision. Order matters: never reveal token state to
    a caller that is not even allowed to ask."""

    if not config.enabled or not config.token:
        return ShutdownDecision(False, 404, "desktop_mode_disabled")
    if not is_loopback_host(client_host):
        return ShutdownDecision(False, 403, "loopback_only")
    presented = _bearer_token(authorization)
    if presented is None:
        return ShutdownDecision(False, 401, "missing_token")
    if not hmac.compare_digest(presented.encode("utf-8"), config.token.encode("utf-8")):
        return ShutdownDecision(False, 403, "invalid_token")
    return ShutdownDecision(True, 202, "accepted")


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def build_desktop_control_router(
    config: DesktopShutdownConfig,
    request_exit: Callable[[], None],
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    state = {"requested": False}

    @router.post(SHUTDOWN_PATH, status_code=202)
    async def desktop_shutdown(request: Request) -> dict[str, object]:
        client_host = request.client.host if request.client else None
        decision = authorize_desktop_shutdown(
            config, request.headers.get("authorization"), client_host
        )
        if not decision.allowed:
            LOGGER.warning(
                "desktop_shutdown_rejected reason=%s client=%s", decision.reason, client_host
            )
            raise HTTPException(status_code=decision.status_code, detail=decision.reason)
        already = state["requested"]
        state["requested"] = True
        if not already:
            LOGGER.warning("desktop_shutdown_accepted source=desktop_controller")
            request_exit()
        return {
            "status": "stopping",
            "already_requested": already,
            "lifespan_cleanup": "pending",
        }

    @router.get(SESSION_PATH)
    async def desktop_session(request: Request) -> dict[str, object]:
        client_host = request.client.host if request.client else None
        if not config.enabled:
            raise HTTPException(status_code=404, detail="desktop_mode_disabled")
        if not is_loopback_host(client_host):
            raise HTTPException(status_code=403, detail="loopback_only")
        return {"desktop_mode": True, "session_id": config.session_id}

    return router
