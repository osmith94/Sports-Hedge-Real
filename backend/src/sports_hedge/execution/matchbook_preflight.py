"""Read-only Matchbook operator check. This module never places or cancels an offer.

Balance authority is ``GET /edge/rest/account/balance`` on
``MatchbookHttpExecutionTransport``, after that transport's ``_login``.
Official Get Balance (developers.matchbook.com, ``get-new-wallet-balance``)
returns one object:

    id, balance, exposure, commission-reserve, free-funds

There is no documented currency property.

``free-funds`` is the spendable betting cash. ``balance`` is the account
balance and still includes amounts locked in ``exposure`` and
``commission-reserve``, so it is not the spendable field. The account ``id``
is never copied out.

Currency is reported only when that object actually contains a three-letter
``currency`` code. A missing currency is not replaced with
``Settings.matchbook_currency``. An explicit code that differs from the
configured execution currency fails closed. Absence is not a contradiction:
the official payload cannot prove a match, and readiness does not invent one.
Generic readiness is spendable ``free-funds`` greater than zero, with no
explicit currency conflict. It does not require execution to be enabled and
does not invent a minimum canary stake.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_hedge.config import Settings

# Venue field for cash that can be staked. Not ``balance``.
SPENDABLE_BALANCE_FIELD = "free-funds"
_CURRENCY_FIELD = "currency"


def snapshot_from_balance_body(
    body: Any,
    *,
    configured_currency: str,
    authenticated: bool,
    observed: bool,
) -> dict[str, Any]:
    """Sanitized balance facts. Never includes the session token or account id."""

    currency, currency_compatible = _currency(body if observed else None, configured_currency)
    free_funds = _spendable(body) if observed else None
    balance_readable = free_funds is not None
    balance_positive = free_funds is not None and free_funds > 0
    # Missing currency is unknown, not a claimed match. Only an explicit
    # mismatch (currency_compatible is False) blocks readiness.
    transport_ready = (
        authenticated
        and balance_readable
        and balance_positive
        and currency_compatible is not False
    )
    snapshot: dict[str, Any] = {
        "authenticated_read": authenticated,
        "balance_observed": observed and isinstance(body, dict),
        "balance_readable": balance_readable,
        "balance_positive": balance_positive,
        "currency": currency,
        "currency_compatible": currency_compatible,
        "transport_ready": transport_ready,
    }
    if free_funds is not None:
        snapshot["free_funds"] = format(free_funds, "f")
    return snapshot


async def collect_matchbook_preflight(
    settings: Settings,
    *,
    account: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Operator report. Does not submit, edit, or cancel an offer."""

    username_configured = bool((settings.matchbook_username or "").strip())
    password_configured = bool(settings.matchbook_password)
    report: dict[str, Any] = {
        "mode": settings.sports_hedge_mode,
        "execution_enabled": settings.sports_hedge_execution_enabled is True,
        "username_configured": username_configured,
        "password_configured": password_configured,
        "authenticated_read": False,
        "balance_observed": False,
        "balance_readable": False,
        "balance_positive": False,
        "currency": None,
        "currency_compatible": None,
        "transport_ready": False,
    }
    if not username_configured or not password_configured:
        return report
    if account is None:
        from sports_hedge.execution.matchbook_http import MatchbookHttpExecutionTransport

        transport = MatchbookHttpExecutionTransport(settings)
        try:
            snapshot = await transport.account_snapshot()
        finally:
            await transport.aclose()
    else:
        try:
            snapshot = await account()
        except Exception:  # noqa: BLE001 — a probe failure is an unread account
            snapshot = {}
    for key in (
        "authenticated_read",
        "balance_observed",
        "balance_readable",
        "balance_positive",
        "currency",
        "currency_compatible",
        "transport_ready",
    ):
        if key in snapshot:
            report[key] = snapshot[key]
    free_funds = snapshot.get("free_funds") if isinstance(snapshot, dict) else None
    if isinstance(free_funds, str) and free_funds:
        report["free_funds"] = free_funds
    return report


def format_matchbook_preflight(report: dict[str, Any]) -> str:
    account_lines = [
        "Account:",
        f"  authenticated read: {'PASS' if report['authenticated_read'] else 'FAIL'}",
        f"  balance readable: {_yes_no(bool(report['balance_readable']))}",
    ]
    if report.get("balance_observed"):
        currency = report.get("currency")
        if isinstance(currency, str) and currency:
            account_lines.append(f"  currency: {currency}")
        elif report.get("currency_compatible") is False:
            account_lines.append("  currency: incompatible")
        else:
            account_lines.append("  currency: unavailable")
    if isinstance(report.get("free_funds"), str):
        account_lines.append(f"  spendable balance: {report['free_funds']}")
        account_lines.append(
            f"  spendable balance positive: {_yes_no(bool(report['balance_positive']))}"
        )
    lines = [
        "SPORTS HEDGE — MATCHBOOK PREFLIGHT",
        "",
        f"Mode: {report['mode']}",
        f"Execution enabled: {str(bool(report['execution_enabled'])).lower()}",
        "",
        "Credentials:",
        f"  username configured: {_yes_no(bool(report['username_configured']))}",
        f"  password configured: {_yes_no(bool(report['password_configured']))}",
        "",
        *account_lines,
        "",
        "Execution transport:",
        f"  ready: {_yes_no(bool(report['transport_ready']))}",
        "",
        "LIVE ORDER SUBMISSION:",
        "  ENABLED" if report["execution_enabled"] else "  DISABLED",
    ]
    return "\n".join(lines)


def _currency(body: Any, configured_currency: str) -> tuple[str | None, bool | None]:
    if not isinstance(body, dict) or _CURRENCY_FIELD not in body:
        return None, None
    raw = body.get(_CURRENCY_FIELD)
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, False
    code = raw.strip().upper()
    if len(code) != 3 or not code.isalpha():
        return None, False
    return code, code == configured_currency.upper()


def _spendable(body: Any) -> Decimal | None:
    if not isinstance(body, dict) or SPENDABLE_BALANCE_FIELD not in body:
        return None
    return _decimal(body.get(SPENDABLE_BALANCE_FIELD))


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def main() -> None:
    import asyncio

    from sports_hedge.config import get_settings

    report = asyncio.run(collect_matchbook_preflight(get_settings()))
    print(format_matchbook_preflight(report))


if __name__ == "__main__":
    main()
