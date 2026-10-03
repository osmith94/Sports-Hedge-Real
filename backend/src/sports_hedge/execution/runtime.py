"""Process-owned execution transports. Credentials are not a transport.

The scanner reads this runtime. An empty runtime cannot submit orders, and
health must not treat stored usernames or key paths as a live order path.
"""

from __future__ import annotations

from sports_hedge.execution.clients import KalshiExecutionClient, MatchbookExecutionClient

_MATCHBOOK_HTTP = "matchbook_http"
_KALSHI_HTTP = "kalshi_http"
_seam_available = False


class ExecutionRuntime:
    """Injected venue execution clients. None means that venue is not armed."""

    def __init__(
        self,
        *,
        matchbook: MatchbookExecutionClient | None = None,
        kalshi: KalshiExecutionClient | None = None,
    ) -> None:
        self.matchbook = matchbook
        self.kalshi = kalshi


_RUNTIME = ExecutionRuntime()


def get_execution_runtime() -> ExecutionRuntime:
    return _RUNTIME


def set_execution_runtime(runtime: ExecutionRuntime | None) -> None:
    """Replace the process runtime. None restores the empty default."""

    global _RUNTIME
    _RUNTIME = runtime if runtime is not None else ExecutionRuntime()


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
    kalshi_armed = transport_armed(runtime.kalshi, kind=_KALSHI_HTTP)
    ready = bool(dispatch_seam_available() and matchbook_armed and kalshi_armed)
    return {
        "live_execution_ready": ready,
        "execution_transport": "armed" if ready else "unavailable",
        "scanner_execution": "live" if ready else "paper",
    }
