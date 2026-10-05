"""Read-only Polymarket operator check. This module never places or cancels an order."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sports_hedge.config import Settings
from sports_hedge.execution.polymarket_buy_readiness import (
    PLATFORM_WIDE_APPROVAL_DIAGNOSTIC,
    platform_wide_approval_diagnostic,
    probe_exchange_v3_collateral,
)
from sports_hedge.execution.polymarket_geoblock import PolymarketEligibility, fetch_geoblock
from sports_hedge.execution.polymarket_sdk import (
    cached_api_creds,
    configured_api_creds,
    open_polymarket_client,
    wallet_config,
)


async def collect_polymarket_preflight(
    settings: Settings,
    *,
    geoblock: Callable[[], Awaitable[PolymarketEligibility]] | None = None,
    account: Callable[[], dict[str, bool | str]] | None = None,
) -> dict[str, Any]:
    eligibility = await (geoblock or (lambda: fetch_geoblock(settings.polymarket_geoblock_url)))()
    wallet = wallet_config(settings)
    creds = configured_api_creds(settings) or cached_api_creds(settings)
    # Cached L2 credentials are valid when they parse. A later balance or
    # open-order failure must not flip this back to invalid.
    l2_auth_valid = creds is not None
    authenticated = False
    balance_readable = False
    trading_ready = False
    trading_readiness_reason: str | None = None
    open_orders_readable = False
    platform_diagnostic: bool | str | None = None
    if account is not None:
        try:
            snapshot = account()
        except Exception:  # noqa: BLE001 — one probe failure must not hide the other signals
            snapshot = {}
        authenticated = bool(snapshot.get("authenticated_read"))
        balance_readable = bool(snapshot.get("balance_readable"))
        trading_ready = bool(snapshot.get("trading_ready"))
        open_orders_readable = bool(snapshot.get("open_orders_readable"))
        reason = snapshot.get("trading_readiness_reason")
        trading_readiness_reason = reason if isinstance(reason, str) and reason else None
        if PLATFORM_WIDE_APPROVAL_DIAGNOSTIC in snapshot:
            platform_diagnostic = snapshot[PLATFORM_WIDE_APPROVAL_DIAGNOSTIC]  # type: ignore[assignment]
    elif wallet is not None and creds is not None:
        client = None
        try:
            client = open_polymarket_client(settings, derive_credentials=False)
            authenticated = True
        except Exception:  # noqa: BLE001 — client open is its own signal
            authenticated = False
            l2_auth_valid = False
        if client is not None:
            balance_readable, trading_ready, trading_readiness_reason = probe_exchange_v3_collateral(
                client
            )
            platform_diagnostic = platform_wide_approval_diagnostic(client)
            try:
                page = client.list_open_orders().first_page()
                open_orders_readable = page is not None
            except Exception:  # noqa: BLE001 — open-order failure stays on that signal
                open_orders_readable = False
            finally:
                client.close()
    transport_ready = (
        wallet is not None
        and creds is not None
        and authenticated
        and balance_readable
        and trading_ready
        and open_orders_readable
    )
    return {
        "mode": settings.sports_hedge_mode,
        "execution_enabled": settings.sports_hedge_execution_enabled is True,
        "eligibility": eligibility,
        "private_key_configured": wallet is not None,
        "wallet_valid": wallet is not None,
        "l2_configured": creds is not None,
        "l2_auth_valid": l2_auth_valid,
        "authenticated_read": authenticated,
        "balance_readable": balance_readable,
        "trading_ready": trading_ready,
        "trading_readiness_reason": trading_readiness_reason,
        "open_orders_readable": open_orders_readable,
        "transport_ready": transport_ready,
        PLATFORM_WIDE_APPROVAL_DIAGNOSTIC: platform_diagnostic,
    }


def format_polymarket_preflight(report: dict[str, Any]) -> str:
    eligibility: PolymarketEligibility = report["eligibility"]
    country = eligibility.country or "unknown"
    allowance = _allowance_line(report)
    lines = [
        "SPORTS HEDGE — POLYMARKET PREFLIGHT",
        "",
        f"Mode: {report['mode']}",
        f"Execution enabled: {str(bool(report['execution_enabled'])).lower()}",
        "",
        "Location eligibility:",
        f"  check reachable: {_yes_no(eligibility.reachable)}",
        f"  blocked: {_blocked(eligibility.blocked)}",
        f"  country code: {country}",
        "",
        "Credentials:",
        f"  private key configured: {_yes_no(report['private_key_configured'])}",
        f"  wallet/signing configuration valid: {_yes_no(report['wallet_valid'])}",
        f"  L2 auth valid: {_yes_no(report['l2_auth_valid'])}",
        "",
        "Account:",
        f"  authenticated read: {'PASS' if report['authenticated_read'] else 'FAIL'}",
        f"  balance readable: {_yes_no(report['balance_readable'])}",
        f"  trading allowance/readiness: {allowance}",
        *_diagnostic_line(report),
        f"  open orders readable: {_yes_no(report['open_orders_readable'])}",
        "",
        "Execution transport:",
        f"  ready: {_yes_no(report['transport_ready'])}",
        "",
        "LIVE ORDER SUBMISSION:",
        "  DISABLED",
    ]
    return "\n".join(lines)


def _allowance_line(report: dict[str, Any]) -> str:
    if report["trading_ready"]:
        return "READY"
    reason = report.get("trading_readiness_reason")
    if isinstance(reason, str) and reason:
        return f"ACTION REQUIRED: {reason}"
    return "ACTION REQUIRED"


def _diagnostic_line(report: dict[str, Any]) -> tuple[str, ...]:
    if PLATFORM_WIDE_APPROVAL_DIAGNOSTIC not in report:
        return ()
    diagnostic = report[PLATFORM_WIDE_APPROVAL_DIAGNOSTIC]
    if diagnostic is None:
        return ()
    if diagnostic is True:
        text = "fully approved"
    elif diagnostic is False:
        text = "not fully approved"
    else:
        text = str(diagnostic)
    return (
        "  platform-wide approvals (diagnostic, not execution readiness): " + text,
    )


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _blocked(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def main() -> None:
    import asyncio

    from sports_hedge.config import get_settings

    report = asyncio.run(collect_polymarket_preflight(get_settings()))
    print(format_polymarket_preflight(report))


if __name__ == "__main__":
    main()
