"""Process-owned execution transports. Credentials are not a transport.

The scanner reads this runtime. An empty runtime cannot submit orders, and
health must not treat stored usernames or key paths as a live order path.
"""

from __future__ import annotations

from sports_hedge.execution.clients import (
    KalshiExecutionClient,
    MatchbookExecutionClient,
    PolymarketExecutionClient,
)

_MATCHBOOK_HTTP = "matchbook_http"
_POLYMARKET_HTTP = "polymarket_http"
_seam_available = False


class ExecutionRuntime:
    """Injected venue execution clients. None means that venue is not armed."""

    def __init__(
        self,
        *,
        matchbook: MatchbookExecutionClient | None = None,
        kalshi: KalshiExecutionClient | None = None,
        polymarket: PolymarketExecutionClient | None = None,
    ) -> None:
        self.matchbook = matchbook
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.explicit = False
        self.configured_signature: str | None = None


_RUNTIME = ExecutionRuntime()


def get_execution_runtime() -> ExecutionRuntime:
    """The one process runtime. Callers must not keep a second copy."""

    return _RUNTIME


def set_execution_runtime(runtime: ExecutionRuntime | None) -> None:
    """Install clients on the process runtime. None clears that installation.

    The object identity of ``get_execution_runtime()`` does not change. Health
    and the scanner therefore cannot diverge by holding different instances.
    An installed runtime is explicit until cleared, so a later settings bind
    does not replace test or operator clients.
    """

    current = _RUNTIME
    if runtime is None:
        current.matchbook = None
        current.kalshi = None
        current.polymarket = None
        current.explicit = False
        current.configured_signature = None
        return
    if runtime is not current:
        current.matchbook = runtime.matchbook
        current.kalshi = runtime.kalshi
        current.polymarket = runtime.polymarket
    current.explicit = True
    current.configured_signature = None


def mark_dispatch_seam_available() -> None:
    global _seam_available
    _seam_available = True


def dispatch_seam_available() -> bool:
    return _seam_available


def transport_armed(client: object | None, *, kind: str) -> bool:
    """True only for an injected non-test transport of the expected kind."""

    if client is None:
        return False
    transport = getattr(client, "_transport", None)
    if transport is None:
        return False
    if bool(getattr(transport, "test_only", True)):
        return False
    return getattr(transport, "transport_kind", "") == kind


def scanner_execution_posture() -> dict[str, bool | str]:
    """Readiness for the Wave-3 scanner seam. Credential strings are ignored."""

    runtime = get_execution_runtime()
    matchbook_armed = transport_armed(runtime.matchbook, kind=_MATCHBOOK_HTTP)
    polymarket_armed = transport_armed(runtime.polymarket, kind=_POLYMARKET_HTTP)
    # The first canary pair is Matchbook + Polymarket. Kalshi stays dispatchable
    # when its own client is installed, and is not required for this flag.
    ready = bool(dispatch_seam_available() and matchbook_armed and polymarket_armed)
    return {
        "live_execution_ready": ready,
        "execution_transport": "armed" if ready else "unavailable",
        "scanner_execution": "live" if ready else "paper",
    }
