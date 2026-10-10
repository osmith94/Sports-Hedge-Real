"""Wave 40 runtime execution arming: OFF at boot, operator ON after preflight.

This is not a separate canary product and does not invent session stakes.
Capital authority stays on existing operator Max Event / Max Opportunity /
Max One-Time settings. Transport readiness is never trading permission.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.chain import PaperFillPlan

OPERATOR_CONFIRMATION_PHRASE = "ARM LIVE CANARY"
_QUANTUM = Decimal("0.01")

_lock = threading.Lock()
_FACTS: LiveCanaryTradingFacts | None = None
_RUNTIME_ARMED = False
_NEW_ORDERS_HALTED = False
_RUNTIME_PREFLIGHT_AT: str | None = None


@dataclass(frozen=True)
class LiveCanaryTradingFacts:
    """Injected or collected read-only trading facts. Not transport readiness."""

    polymarket_geoblock_permitted: bool = False
    polymarket_geoblock_reason: str = "geoblock_not_checked"
    polymarket_credentials_present: bool = False
    polymarket_wallet_valid: bool = False
    polymarket_balance_readable: bool = False
    polymarket_trading_ready: bool = False
    polymarket_trading_reason: str | None = None
    matchbook_credentials_present: bool = False
    matchbook_authenticated: bool = False
    matchbook_free_funds_positive: bool = False
    matchbook_currency_compatible: bool | None = True
    outstanding_unknown_orders: bool = False
    fee_fx_known: bool = True


def reset_live_canary_runtime() -> None:
    global _FACTS, _RUNTIME_ARMED, _NEW_ORDERS_HALTED, _RUNTIME_PREFLIGHT_AT
    with _lock:
        _FACTS = None
        _RUNTIME_ARMED = False
        _NEW_ORDERS_HALTED = False
        _RUNTIME_PREFLIGHT_AT = None


def set_live_canary_trading_facts(facts: LiveCanaryTradingFacts | None) -> None:
    global _FACTS
    _FACTS = facts


def live_canary_trading_facts() -> LiveCanaryTradingFacts | None:
    return _FACTS


def live_runtime_armed() -> bool:
    """Process arm bit. Restarts always start OFF. Healthy ON does not time out."""

    with _lock:
        return bool(_RUNTIME_ARMED) and not _NEW_ORDERS_HALTED


def new_orders_halted() -> bool:
    with _lock:
        return bool(_NEW_ORDERS_HALTED)


def set_live_runtime_arm(armed: bool) -> None:
    global _RUNTIME_ARMED, _NEW_ORDERS_HALTED
    with _lock:
        _RUNTIME_ARMED = bool(armed)
        if armed:
            _NEW_ORDERS_HALTED = False


def heartbeat_live_runtime() -> bool:
    """Optional UI keepalive. Does not expire a healthy ON state."""

    return live_runtime_armed()


def live_canary_enabled(settings: Settings) -> bool:
    """True when the dashboard is runtime-armed or tests opt in via env."""

    return settings.sports_hedge_live_canary_enabled is True or live_runtime_armed()


def authorization_path(settings: Settings) -> Path:
    configured = (settings.sports_hedge_live_canary_authorization_path or "").strip()
    if configured:
        return Path(configured)
    root = (settings.sports_hedge_repo_root or "").strip()
    if root:
        return Path(root) / "logs" / "live-canary-authorization.json"
    return Path("../logs/live-canary-authorization.json")


def load_authorization(settings: Settings) -> dict[str, Any] | None:
    path = authorization_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    phrase = str(payload.get("phrase") or "")
    if phrase != OPERATOR_CONFIRMATION_PHRASE:
        return None
    return payload


def authorization_valid(settings: Settings) -> bool:
    if live_runtime_armed():
        return True
    if not settings.sports_hedge_live_canary_operator_confirmed:
        return False
    return load_authorization(settings) is not None


def live_canary_capture_active(settings: Settings) -> bool:
    """Scanner may auto-capture live without PAPER_AUTOFILL_ENABLED."""

    from sports_hedge.execution.package import execution_armed

    return (
        live_canary_enabled(settings)
        and execution_armed(settings)
        and authorization_valid(settings)
    )


def plan_outlay_gbp(plan: PaperFillPlan) -> Decimal | None:
    """Combined GBP outlay of requested stakes. Missing FX fails closed."""

    rates = {item.currency.upper(): item.gbp_per_unit for item in plan.fx_snapshots}
    total = Decimal("0")
    for leg in plan.legs:
        if leg.requested_stake <= 0:
            continue
        currency = (leg.currency or "").upper()
        if currency == "GBP":
            total += leg.requested_stake
            continue
        rate = rates.get(currency)
        if rate is None:
            return None
        total += leg.requested_stake * rate
    if total <= 0:
        return None
    return total.quantize(_QUANTUM, rounding=ROUND_UP)


def snapshot_payload(plan: PaperFillPlan) -> dict[str, Any] | None:
    raw = plan.execution_snapshot_json
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def operator_limits_payload(settings: Settings) -> dict[str, str]:
    """Status/health limits. Do not open paper_settings.sqlite from GET /health."""

    from sports_hedge.persistence.operator_scanner_settings import (
        bound_runtime_operator_scanner_settings_store,
        env_operator_scanner_settings,
        resolve_operator_scanner_settings,
    )

    store = bound_runtime_operator_scanner_settings_store()
    operator = (
        resolve_operator_scanner_settings(store, settings)
        if store is not None
        else env_operator_scanner_settings(settings)
    )
    return {
        "max_event_gbp": format(operator.max_event_gbp, "f"),
        "max_opportunity_gbp": format(operator.max_opportunity_gbp, "f"),
        "max_one_time_gbp": format(operator.max_one_time_gbp, "f"),
        "min_net_edge": format(operator.min_net_edge, "f"),
    }


def canary_plan_refusal(plan: PaperFillPlan, *, settings: Settings) -> str | None:
    """Price-2 / identity / operator-cap gates that do not need venue I/O."""

    from sports_hedge.persistence.operator_scanner_settings import (
        effective_operator_scanner_settings,
    )

    if not plan.settlement_equivalent or not plan.eligible_for_paper_simulation:
        return "canary_settlement_not_live_eligible"
    if not plan.execution_authoritative:
        return "canary_price2_not_authoritative"
    payload = snapshot_payload(plan)
    if payload is None or payload.get("accepted") is not True:
        return "canary_price2_not_accepted"
    max_skew = int(settings.paper_execution_max_snapshot_skew_ms)
    snapshot_max_skew = payload.get("max_skew_ms", max_skew)
    skew = payload.get("skew_ms")
    if skew is None:
        return "canary_price2_skew_unknown"
    try:
        allowed_skew = int(snapshot_max_skew if snapshot_max_skew is not None else max_skew)
        allowed_skew = min(allowed_skew, max_skew)
        if int(skew) > allowed_skew:
            return "canary_price2_skew"
    except (TypeError, ValueError):
        return "canary_price2_skew_unknown"
    max_age = int(settings.paper_entry_max_quote_age_ms)
    snapshot_max_age = payload.get("max_quote_age_ms", max_age)
    age = payload.get("oldest_quote_age_ms")
    if age is None:
        return "canary_price2_stale"
    try:
        allowed_age = int(snapshot_max_age if snapshot_max_age is not None else max_age)
        allowed_age = min(allowed_age, max_age)
        if int(age) > allowed_age:
            return "canary_price2_stale"
    except (TypeError, ValueError):
        return "canary_price2_stale"
    operator = effective_operator_scanner_settings(settings)
    net = payload.get("net_edge")
    try:
        if net is None or Decimal(str(net)) < operator.min_net_edge:
            return "canary_min_net_edge"
    except (InvalidOperation, ValueError):
        return "canary_min_net_edge"
    costs = payload.get("fee_snapshots") or payload.get("venue_costs")
    fx = payload.get("fx_rates")
    if costs in (None, []) or fx in (None, []):
        return "canary_fee_fx_unknown"
    gbp = plan_outlay_gbp(plan)
    if gbp is None:
        return "canary_outlay_unknown"
    if gbp > operator.max_one_time_gbp:
        return "operator_max_one_time"
    if gbp > operator.max_opportunity_gbp:
        return "operator_max_opportunity"
    if gbp > operator.max_event_gbp:
        return "operator_max_event"
    minima = _native_minima_gbp(payload)
    if minima is not None and minima > operator.max_one_time_gbp:
        return "native_minimum_exceeds_operator_cap"
    if minima is not None and minima > gbp:
        return "native_minimum_exceeds_plan"
    return None


def _native_minima_gbp(payload: dict[str, Any]) -> Decimal | None:
    orders = payload.get("frozen_orders")
    if not isinstance(orders, list) or not orders:
        return None
    total = Decimal("0")
    known = False
    for item in orders:
        if not isinstance(item, dict):
            continue
        venue = str(item.get("venue") or "")
        minimum = item.get("minimum_order_size")
        if minimum is None and venue == VenueName.MATCHBOOK.value:
            minimum = item.get("native_stake")
        if minimum is None:
            continue
        try:
            amount = Decimal(str(minimum))
        except (InvalidOperation, ValueError):
            return None
        currency = str(item.get("currency") or "GBP").upper()
        if venue == VenueName.POLYMARKET.value:
            rate = _fx_rate(payload, "USD") if currency != "GBP" else Decimal("1")
            if rate is None:
                return None
            total += amount * rate
        else:
            total += amount
        known = True
    if not known:
        return None
    return total.quantize(_QUANTUM, rounding=ROUND_UP)


def _fx_rate(payload: dict[str, Any], currency: str) -> Decimal | None:
    rates = payload.get("fx_rates")
    if not isinstance(rates, list):
        return None
    for item in rates:
        if not isinstance(item, dict):
            continue
        if str(item.get("currency") or "").upper() != currency.upper():
            continue
        raw = item.get("gbp_per_unit")
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            return None
    return None


def trading_permission_reasons(
    settings: Settings,
    facts: LiveCanaryTradingFacts | None,
    *,
    for_arm: bool = False,
    require_facts: bool = True,
) -> list[str]:
    reasons: list[str] = []
    if settings.sports_hedge_mode != "real":
        reasons.append("not_real_mode")
    if not for_arm:
        from sports_hedge.execution.package import execution_armed

        if new_orders_halted():
            reasons.append("canary_halt_new_orders")
        if not live_canary_enabled(settings):
            reasons.append("canary_unarmed")
        if not execution_armed(settings):
            reasons.append("execution_disabled")
        if not settings.sports_hedge_live_canary_operator_confirmed and not live_runtime_armed():
            reasons.append("canary_operator_unconfirmed")
        if not authorization_valid(settings):
            reasons.append("canary_authorization_missing")
    if facts is None:
        if require_facts:
            reasons.append("canary_preflight_not_collected")
        return reasons
    if not facts.polymarket_geoblock_permitted:
        reasons.append(facts.polymarket_geoblock_reason or "geoblock_blocked")
    if not facts.polymarket_credentials_present or not facts.polymarket_wallet_valid:
        reasons.append("polymarket_credentials_missing")
    if not facts.polymarket_balance_readable:
        reasons.append("polymarket_balance_unreadable")
    if not facts.polymarket_trading_ready:
        reasons.append(facts.polymarket_trading_reason or "polymarket_allowance_missing")
    if not facts.matchbook_credentials_present:
        reasons.append("matchbook_credentials_missing")
    if not facts.matchbook_authenticated:
        reasons.append("matchbook_unauthenticated")
    if not facts.matchbook_free_funds_positive:
        reasons.append("matchbook_free_funds")
    if facts.matchbook_currency_compatible is False:
        reasons.append("matchbook_currency_mismatch")
    if facts.outstanding_unknown_orders:
        reasons.append("outstanding_unknown_orders")
    if not facts.fee_fx_known:
        reasons.append("canary_fee_fx_unknown")
    return reasons


async def collect_live_canary_facts(settings: Settings) -> LiveCanaryTradingFacts:
    """Read-only venue probes. Never submits. Not used by GET /health."""

    from sports_hedge.execution.matchbook_preflight import collect_matchbook_preflight
    from sports_hedge.execution.polymarket_preflight import collect_polymarket_preflight

    polymarket = await collect_polymarket_preflight(settings)
    matchbook = await collect_matchbook_preflight(settings)
    eligibility = polymarket.get("eligibility")
    permitted = bool(getattr(eligibility, "permitted", False))
    geo_reason = str(getattr(eligibility, "reason", "geoblock_not_checked") or "geoblock_not_checked")
    allowance_reason = polymarket.get("trading_readiness_reason")
    return LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=permitted,
        polymarket_geoblock_reason=geo_reason,
        polymarket_credentials_present=bool(polymarket.get("private_key_configured")),
        polymarket_wallet_valid=bool(polymarket.get("wallet_valid")),
        polymarket_balance_readable=bool(polymarket.get("balance_readable")),
        polymarket_trading_ready=bool(polymarket.get("trading_ready")),
        polymarket_trading_reason=allowance_reason if isinstance(allowance_reason, str) else None,
        matchbook_credentials_present=bool(
            matchbook.get("username_configured") and matchbook.get("password_configured")
        ),
        matchbook_authenticated=bool(matchbook.get("authenticated_read")),
        matchbook_free_funds_positive=bool(matchbook.get("balance_positive")),
        matchbook_currency_compatible=matchbook.get("currency_compatible"),
        fee_fx_known=True,
    )


def resolve_trading_facts(settings: Settings) -> LiveCanaryTradingFacts | None:
    """Injected test facts win. Otherwise collect once at submit, never from /health."""

    if _FACTS is not None:
        return _FACTS
    if not live_canary_enabled(settings):
        return None
    from sports_hedge.execution.dispatch import run_blocking

    return run_blocking(collect_live_canary_facts(settings))


def canary_submit_refusal(
    plan: PaperFillPlan,
    *,
    settings: Settings,
    tranche_id: str | None = None,
    facts: LiveCanaryTradingFacts | None = None,
) -> str | None:
    """Refuse a live submit that the runtime-arm path must still block.

    ``live_execution_ready`` is intentionally ignored.
    """

    del tranche_id
    if not live_canary_enabled(settings):
        return None
    resolved = facts if facts is not None else resolve_trading_facts(settings)
    reasons = trading_permission_reasons(settings, resolved)
    if reasons:
        return reasons[0]
    return canary_plan_refusal(plan, settings=settings)


def public_canary_status(settings: Settings) -> dict[str, Any]:
    facts = _FACTS
    permission_reasons = trading_permission_reasons(
        settings, facts, require_facts=facts is not None
    )
    runtime_on = live_runtime_armed()
    if new_orders_halted() and "canary_halt_new_orders" not in permission_reasons:
        permission_reasons.append("canary_halt_new_orders")
    ready = not permission_reasons and live_canary_enabled(settings)
    limits = operator_limits_payload(settings)
    return {
        "enabled": live_canary_enabled(settings),
        "runtime_armed": runtime_on,
        "execution_toggle": "ON" if runtime_on else "OFF",
        "operator_confirmed": bool(
            settings.sports_hedge_live_canary_operator_confirmed or runtime_on
        ),
        "authorization_valid": authorization_valid(settings),
        "capture_active": live_canary_capture_active(settings),
        "halt_new_orders": (not runtime_on) or new_orders_halted(),
        "operator_limits": limits,
        "max_event_gbp": limits["max_event_gbp"],
        "max_opportunity_gbp": limits["max_opportunity_gbp"],
        "max_one_time_gbp": limits["max_one_time_gbp"],
        "min_net_edge": limits["min_net_edge"],
        "trading_permission": ready,
        "not_ready_reasons": permission_reasons,
        "live_execution_ready_is_not_permission": True,
        "paper_autofill_enabled": settings.paper_autofill_enabled,
        "disarm_does_not_cancel_existing": True,
        "preflight_at": _RUNTIME_PREFLIGHT_AT,
        "label": "EXECUTION ON" if runtime_on else "EXECUTION OFF",
        "no_session_cap": True,
        "no_one_opportunity_limit": True,
    }


async def arm_live_runtime(settings: Settings, *, phrase: str) -> dict[str, Any]:
    global _RUNTIME_PREFLIGHT_AT
    if phrase != OPERATOR_CONFIRMATION_PHRASE:
        return {
            **public_canary_status(settings),
            "armed": False,
            "not_ready_reasons": ["canary_operator_unconfirmed"],
        }
    facts = _FACTS if _FACTS is not None else await collect_live_canary_facts(settings)
    set_live_canary_trading_facts(facts)
    _RUNTIME_PREFLIGHT_AT = datetime.now(UTC).isoformat()
    reasons = trading_permission_reasons(settings, facts, for_arm=True)
    if reasons:
        set_live_runtime_arm(False)
        status = public_canary_status(settings)
        status["armed"] = False
        status["not_ready_reasons"] = reasons
        status["preflight_at"] = _RUNTIME_PREFLIGHT_AT
        return status
    set_live_runtime_arm(True)
    status = public_canary_status(settings)
    status["armed"] = True
    return status


def disarm_live_runtime(settings: Settings) -> dict[str, Any]:
    """Stop new submits. Does not cancel orders already sent to a venue."""

    global _NEW_ORDERS_HALTED, _RUNTIME_ARMED
    with _lock:
        _RUNTIME_ARMED = False
        _NEW_ORDERS_HALTED = True
    return public_canary_status(settings)
