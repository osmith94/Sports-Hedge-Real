"""V2 BUY collateral authority. One spender, one allowance check.

polymarket-client 0.12.0 resolves a V2 token order's exchange through
``resolve_order_exchange_address`` to ``EnvironmentConfig.exchange_v3``.
This module reads that field from the client's own environment config.
It does not copy an address from documentation and it does not treat any
other allowance, or ``TradingApprovalsState.is_fully_approved``, as permission
to buy.

A live order compares collateral balance and that spender's allowance with
the translated stake. A snapshot or preflight with no order amount requires
a positive collateral balance and a positive allowance for that same spender.
It does not invent a minimum order size. Opening SELL allowance is not read
here.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping

from sports_hedge.execution.polymarket_sdk import NOT_READY

_BASE = Decimal(10) ** 6
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Report key only. Transport readiness must not read it.
PLATFORM_WIDE_APPROVAL_DIAGNOSTIC = (
    "platform_wide_approval_diagnostic_not_execution_readiness"
)
EXCHANGE_V3_UNRESOLVED = "exchange_v3 spender cannot be resolved"
EXCHANGE_V3_ALLOWANCE_BELOW_ORDER = "collateral allowance is below the order"
EXCHANGE_V3_ALLOWANCE_NOT_POSITIVE = "exchange_v3 collateral allowance is not positive"
COLLATERAL_BALANCE_BELOW_ORDER = "collateral balance is below the order"
COLLATERAL_BALANCE_NOT_POSITIVE = "collateral balance is not positive"


def resolve_exchange_v3_spender(client: object) -> str | None:
    """Return the client's V2 exchange spender, or None when it cannot be resolved.

    The bound ``_ctx.environment_config`` is the config the SDK signs against.
    The public ``environment`` is the same authority when the context is absent.
    """

    config = _client_environment_config(client)
    if config is None:
        return None
    address = getattr(config, "exchange_v3", None)
    if not isinstance(address, str):
        return None
    stripped = address.strip()
    if _EVM_ADDRESS.fullmatch(stripped) is None:
        return None
    return stripped


def collateral_base_units(spend: Decimal) -> int:
    """pUSD base units for a translated native stake. Never rounds the stake up."""

    return int((spend * _BASE).to_integral_value(rounding=ROUND_DOWN))


def matched_collateral_allowance(allowances: object, spender: str) -> int | None:
    """Allowance for this spender only. A missing entry is zero.

    Matching is case-insensitive, which is how polymarket-client 0.12.0
    selects ``_allowance_for_spender``. Any other key is ignored. A value
    that is not an integer fails closed as None.
    """

    if not isinstance(allowances, Mapping):
        return None
    target = spender.lower()
    for key, value in allowances.items():
        if isinstance(key, str) and key.lower() == target:
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return value
    return 0


def read_collateral_balance(client: Any) -> Any:
    """Collateral only. Conditional-token allowance is a SELL concern."""

    return client.get_balance_allowance(asset_type="COLLATERAL")


def v2_buy_readiness(client: object, spend: Decimal) -> str | None:
    """Order-specific V2 BUY gate. None means this stake can be funded.

    Checks, in order: the exchange_v3 spender resolves, collateral balance
    covers ``spend``, and that spender's collateral allowance covers ``spend``.
    Platform-wide approval state is not consulted.
    """

    spender = resolve_exchange_v3_spender(client)
    if spender is None:
        return f"{NOT_READY}: {EXCHANGE_V3_UNRESOLVED}"
    balance = read_collateral_balance(client)
    required = collateral_base_units(spend)
    if int(balance.balance) < required:
        return f"{NOT_READY}: {COLLATERAL_BALANCE_BELOW_ORDER}"
    held = matched_collateral_allowance(getattr(balance, "allowances", None), spender)
    if held is None or held < required:
        return f"{NOT_READY}: {EXCHANGE_V3_ALLOWANCE_BELOW_ORDER}"
    return None


def probe_exchange_v3_collateral(client: object) -> tuple[bool, bool, str | None]:
    """No-order probe: (balance readable, generic V2 collateral ready, reason).

    Generic readiness needs a readable collateral balance above zero and a
    positive exchange_v3 collateral allowance. It is not an order-sized check
    and it does not read platform-wide approvals.
    """

    try:
        balance = read_collateral_balance(client)
    except Exception:  # noqa: BLE001 — an unreadable balance is its own signal
        return False, False, "collateral balance could not be read"
    if balance is None:
        return False, False, "collateral balance could not be read"
    try:
        held_balance = int(balance.balance)
    except (TypeError, ValueError):
        return False, False, "collateral balance could not be read"
    if resolve_exchange_v3_spender(client) is None:
        return True, False, EXCHANGE_V3_UNRESOLVED
    if held_balance <= 0:
        return True, False, COLLATERAL_BALANCE_NOT_POSITIVE
    if not exchange_v3_collateral_allowance_is_positive(client, balance):
        return True, False, EXCHANGE_V3_ALLOWANCE_NOT_POSITIVE
    return True, True, None


def exchange_v3_collateral_allowance_is_positive(client: object, balance: object) -> bool:
    """True only when this balance object's exchange_v3 collateral allowance is positive."""

    spender = resolve_exchange_v3_spender(client)
    if spender is None or balance is None:
        return False
    held = matched_collateral_allowance(getattr(balance, "allowances", None), spender)
    return held is not None and held > 0


def platform_wide_approval_diagnostic(client: object) -> bool | str:
    """``is_fully_approved`` as diagnostic text. Never a BUY or transport gate."""

    try:
        state = client.get_trading_approvals_state()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — diagnostic failure must not change readiness
        return "unavailable"
    return bool(getattr(state, "is_fully_approved", False))


def _client_environment_config(client: object) -> object | None:
    context = getattr(client, "_ctx", None)
    bound = getattr(context, "environment_config", None)
    if _names_exchange_v3(bound):
        return bound
    environment = getattr(client, "environment", None)
    if environment is None:
        return None
    try:
        from polymarket._internal.environment import get_environment_config
    except ImportError:
        return None
    try:
        resolved = get_environment_config(environment)
    except Exception:  # noqa: BLE001 — an unreadable environment fails closed
        return None
    if _names_exchange_v3(resolved):
        return resolved
    return None


def _names_exchange_v3(config: object) -> bool:
    address = getattr(config, "exchange_v3", None)
    return isinstance(address, str) and bool(address.strip())
