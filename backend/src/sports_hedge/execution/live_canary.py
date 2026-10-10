"""Wave 40 opt-in live canary: authorization, durable budget, halt, preflight.

This is not the dry-run Real launcher. Default configuration stays disarmed.
Caps are constants so an environment toggle cannot silently enlarge them.
Transport readiness (``live_execution_ready``) is never trading permission.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.trades import OPENING_TRANCHE_ID

OPERATOR_CONFIRMATION_PHRASE = "ARM LIVE CANARY"
DEFAULT_SESSION_ID = "wave40-live-canary"
ARM_LEASE_SECONDS = 45
OPENING_TRANCHE_GBP = Decimal("3.00")
SESSION_CAP_GBP = Decimal("5.00")
MAX_OPPORTUNITIES = 1
MIN_NET_EDGE = Decimal("0.002")
MAX_SKEW_MS = 500
MAX_QUOTE_AGE_MS = 2000
CANARY_VENUES = frozenset({VenueName.MATCHBOOK, VenueName.POLYMARKET})

_QUANTUM = Decimal("0.01")
_CREATE_SESSION = """
CREATE TABLE IF NOT EXISTS live_canary_session (
    session_id TEXT PRIMARY KEY,
    halt_new_orders INTEGER NOT NULL DEFAULT 0,
    authorized_opportunity_id TEXT,
    blocked_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_CREATE_RESERVATION = """
CREATE TABLE IF NOT EXISTS live_canary_reservations (
    reservation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    package_id TEXT NOT NULL UNIQUE,
    opportunity_id TEXT NOT NULL,
    tranche_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    gbp TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_lock = threading.Lock()
_STORE: LiveCanaryStore | None = None
_FACTS: LiveCanaryTradingFacts | None = None
_RUNTIME_ARMED = False
_RUNTIME_HEARTBEAT: datetime | None = None
_RUNTIME_SESSION_ID = DEFAULT_SESSION_ID
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


@dataclass
class LiveCanaryReservation:
    ok: bool
    reason: str | None = None
    reservation_id: str | None = None
    gbp: Decimal = Decimal("0")


class LiveCanaryStore:
    """SQLite session budget. BEGIN IMMEDIATE serializes scanner-lane races."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = None if path is None else str(path)
        self._memory_session: dict[str, Any] | None = None
        self._memory_rows: dict[str, dict[str, Any]] = {}
        if self._path is not None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._connect().close()

    def _connect(self) -> sqlite3.Connection:
        assert self._path is not None
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(_CREATE_SESSION)
        connection.execute(_CREATE_RESERVATION)
        return connection

    def ensure_session(self, session_id: str, *, now: datetime | None = None) -> None:
        when = _iso(now or datetime.now(UTC))
        if self._path is None:
            if self._memory_session is None or self._memory_session["session_id"] != session_id:
                self._memory_session = {
                    "session_id": session_id,
                    "halt_new_orders": 0,
                    "authorized_opportunity_id": None,
                    "blocked_reason": None,
                    "created_at": when,
                    "updated_at": when,
                }
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO live_canary_session (
                    session_id, halt_new_orders, authorized_opportunity_id, blocked_reason,
                    created_at, updated_at
                ) VALUES (?, 0, NULL, NULL, ?, ?)
                """,
                (session_id, when, when),
            )

    def halt_new_orders(self, session_id: str, *, reason: str, now: datetime | None = None) -> None:
        self.ensure_session(session_id, now=now)
        when = _iso(now or datetime.now(UTC))
        if self._path is None:
            assert self._memory_session is not None
            self._memory_session["halt_new_orders"] = 1
            self._memory_session["blocked_reason"] = reason
            self._memory_session["updated_at"] = when
            return
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE live_canary_session
                SET halt_new_orders = 1, blocked_reason = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (reason, when, session_id),
            )

    def session_snapshot(self, session_id: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        if self._path is None:
            assert self._memory_session is not None
            rows = [row for row in self._memory_rows.values() if row["session_id"] == session_id]
            return _session_public(self._memory_session, rows)
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM live_canary_session WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM live_canary_reservations WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            return _session_public(dict(session), [dict(row) for row in rows])

    def reserve(
        self,
        *,
        session_id: str,
        package_id: str,
        opportunity_id: str,
        tranche_id: str,
        gbp: Decimal,
        kind: str,
        now: datetime | None = None,
    ) -> LiveCanaryReservation:
        self.ensure_session(session_id, now=now)
        when = now or datetime.now(UTC)
        if gbp <= 0:
            return LiveCanaryReservation(ok=False, reason="canary_outlay_unknown")
        if self._path is None:
            with _lock:
                return self._reserve_memory(
                    session_id=session_id,
                    package_id=package_id,
                    opportunity_id=opportunity_id,
                    tranche_id=tranche_id,
                    gbp=gbp,
                    kind=kind,
                    when=when,
                )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = dict(
                connection.execute(
                    "SELECT * FROM live_canary_session WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            )
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM live_canary_reservations WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
            ]
            refused = _reserve_refusal(session, rows, opportunity_id=opportunity_id, gbp=gbp, kind=kind)
            if refused is not None:
                connection.execute("ROLLBACK")
                return LiveCanaryReservation(ok=False, reason=refused)
            reservation_id = package_id
            try:
                connection.execute(
                    """
                    INSERT INTO live_canary_reservations (
                        reservation_id, session_id, package_id, opportunity_id, tranche_id,
                        kind, gbp, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                    """,
                    (
                        reservation_id,
                        session_id,
                        package_id,
                        opportunity_id,
                        tranche_id,
                        kind,
                        format(gbp, "f"),
                        _iso(when),
                        _iso(when),
                    ),
                )
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return LiveCanaryReservation(ok=False, reason="canary_duplicate_package")
            if session["authorized_opportunity_id"] is None:
                connection.execute(
                    """
                    UPDATE live_canary_session
                    SET authorized_opportunity_id = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (opportunity_id, _iso(when), session_id),
                )
            connection.execute("COMMIT")
            return LiveCanaryReservation(
                ok=True, reservation_id=reservation_id, gbp=gbp
            )

    def _reserve_memory(
        self,
        *,
        session_id: str,
        package_id: str,
        opportunity_id: str,
        tranche_id: str,
        gbp: Decimal,
        kind: str,
        when: datetime,
    ) -> LiveCanaryReservation:
        assert self._memory_session is not None
        rows = [row for row in self._memory_rows.values() if row["session_id"] == session_id]
        refused = _reserve_refusal(
            self._memory_session, rows, opportunity_id=opportunity_id, gbp=gbp, kind=kind
        )
        if refused is not None:
            return LiveCanaryReservation(ok=False, reason=refused)
        if package_id in self._memory_rows:
            return LiveCanaryReservation(ok=False, reason="canary_duplicate_package")
        reservation_id = package_id
        self._memory_rows[reservation_id] = {
            "reservation_id": reservation_id,
            "session_id": session_id,
            "package_id": package_id,
            "opportunity_id": opportunity_id,
            "tranche_id": tranche_id,
            "kind": kind,
            "gbp": format(gbp, "f"),
            "status": "reserved",
            "created_at": _iso(when),
            "updated_at": _iso(when),
        }
        if self._memory_session["authorized_opportunity_id"] is None:
            self._memory_session["authorized_opportunity_id"] = opportunity_id
            self._memory_session["updated_at"] = _iso(when)
        return LiveCanaryReservation(ok=True, reservation_id=reservation_id, gbp=gbp)

    def complete(
        self,
        reservation_id: str,
        *,
        status: str,
        now: datetime | None = None,
    ) -> None:
        when = _iso(now or datetime.now(UTC))
        if self._path is None:
            row = self._memory_rows.get(reservation_id)
            if row is None:
                return
            row["status"] = status
            row["updated_at"] = when
            if status in {"unknown", "partial", "unhedged"} and self._memory_session is not None:
                self._memory_session["blocked_reason"] = f"canary_{status}_blocks_new"
                self._memory_session["updated_at"] = when
            return
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE live_canary_reservations
                SET status = ?, updated_at = ?
                WHERE reservation_id = ?
                """,
                (status, when, reservation_id),
            )
            if status in {"unknown", "partial", "unhedged"}:
                session = connection.execute(
                    "SELECT session_id FROM live_canary_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                ).fetchone()
                if session is not None:
                    connection.execute(
                        """
                        UPDATE live_canary_session
                        SET blocked_reason = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (f"canary_{status}_blocks_new", when, session["session_id"]),
                    )

    def release(self, reservation_id: str) -> None:
        """Drop a reservation that never reached a venue write."""

        if self._path is None:
            self._memory_rows.pop(reservation_id, None)
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM live_canary_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )


def _reserve_refusal(
    session: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    opportunity_id: str,
    gbp: Decimal,
    kind: str,
) -> str | None:
    if int(session.get("halt_new_orders") or 0):
        return "canary_halt_new_orders"
    blocked = session.get("blocked_reason")
    if blocked:
        return str(blocked)
    if any(row["status"] in {"reserved", "unknown", "partial", "unhedged"} for row in rows):
        return "canary_in_flight_or_unknown"
    authorized = session.get("authorized_opportunity_id")
    opportunities = {row["opportunity_id"] for row in rows}
    if authorized and authorized != opportunity_id:
        return "canary_second_opportunity"
    if authorized is None and opportunities and opportunity_id not in opportunities:
        return "canary_second_opportunity"
    if kind == "opening":
        if any(row["kind"] == "opening" for row in rows):
            return "canary_opening_already_reserved"
        if gbp > OPENING_TRANCHE_GBP:
            return "canary_opening_cap"
    else:
        if authorized is None and not any(row["kind"] == "opening" for row in rows):
            return "canary_topup_without_opening"
        if authorized not in {None, opportunity_id}:
            return "canary_second_opportunity"
        if not any(
            row["kind"] == "opening" and row["status"] == "filled" for row in rows
        ):
            return "canary_topup_requires_complete_hedge"
    used = sum((_decimal(row["gbp"]) for row in rows), Decimal("0"))
    if used + gbp > SESSION_CAP_GBP:
        return "canary_session_cap"
    return None


def _session_public(session: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    used = sum((_decimal(row["gbp"]) for row in rows), Decimal("0"))
    return {
        "session_id": session["session_id"],
        "halt_new_orders": bool(int(session.get("halt_new_orders") or 0)),
        "authorized_opportunity_id": session.get("authorized_opportunity_id"),
        "blocked_reason": session.get("blocked_reason"),
        "reserved_gbp": format(used, "f"),
        "reservations": [
            {
                "reservation_id": row["reservation_id"],
                "package_id": row["package_id"],
                "opportunity_id": row["opportunity_id"],
                "tranche_id": row["tranche_id"],
                "kind": row["kind"],
                "gbp": row["gbp"],
                "status": row["status"],
            }
            for row in rows
        ],
    }


def get_live_canary_store(settings: Settings) -> LiveCanaryStore:
    global _STORE
    path = (settings.sports_hedge_live_canary_db_path or "").strip() or None
    with _lock:
        if _STORE is None or getattr(_STORE, "_path", None) != path:
            _STORE = LiveCanaryStore(path)
        return _STORE


def reset_live_canary_runtime() -> None:
    global _STORE, _FACTS, _RUNTIME_ARMED, _RUNTIME_HEARTBEAT, _RUNTIME_PREFLIGHT_AT
    with _lock:
        _STORE = None
        _FACTS = None
        _RUNTIME_ARMED = False
        _RUNTIME_HEARTBEAT = None
        _RUNTIME_PREFLIGHT_AT = None


def set_live_canary_trading_facts(facts: LiveCanaryTradingFacts | None) -> None:
    global _FACTS
    _FACTS = facts


def live_canary_trading_facts() -> LiveCanaryTradingFacts | None:
    return _FACTS


def live_runtime_armed() -> bool:
    """Process arm bit. Expired leases fail closed. Restarts always start OFF."""

    global _RUNTIME_ARMED, _RUNTIME_HEARTBEAT
    with _lock:
        if not _RUNTIME_ARMED:
            return False
        if _RUNTIME_HEARTBEAT is None:
            _RUNTIME_ARMED = False
            return False
        age = (datetime.now(UTC) - _RUNTIME_HEARTBEAT).total_seconds()
        if age > ARM_LEASE_SECONDS:
            _RUNTIME_ARMED = False
            return False
        return True


def set_live_runtime_arm(
    armed: bool,
    *,
    session_id: str = DEFAULT_SESSION_ID,
    heartbeat: datetime | None = None,
) -> None:
    global _RUNTIME_ARMED, _RUNTIME_HEARTBEAT, _RUNTIME_SESSION_ID
    with _lock:
        _RUNTIME_ARMED = bool(armed)
        _RUNTIME_SESSION_ID = session_id or DEFAULT_SESSION_ID
        _RUNTIME_HEARTBEAT = heartbeat or (datetime.now(UTC) if armed else None)


def heartbeat_live_runtime() -> bool:
    global _RUNTIME_HEARTBEAT
    if not live_runtime_armed():
        return False
    with _lock:
        _RUNTIME_HEARTBEAT = datetime.now(UTC)
    return True


def live_canary_session_id(settings: Settings) -> str:
    configured = (settings.sports_hedge_live_canary_session_id or "").strip()
    if configured:
        return configured
    return _RUNTIME_SESSION_ID


def live_canary_enabled(settings: Settings) -> bool:
    """Budget/canary gates apply when the dashboard is runtime-armed or tests opt in."""

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
    session_id = str(payload.get("session_id") or "").strip()
    if phrase != OPERATOR_CONFIRMATION_PHRASE or not session_id:
        return None
    expected = (settings.sports_hedge_live_canary_session_id or "").strip()
    if expected and session_id != expected:
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


def canary_kind(tranche_id: str) -> str:
    return "opening" if tranche_id == OPENING_TRANCHE_ID else "topup"


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


def canary_plan_refusal(plan: PaperFillPlan, *, tranche_id: str) -> str | None:
    """Price-2 / identity / size gates that do not need venue I/O."""

    if not plan.settlement_equivalent or not plan.eligible_for_paper_simulation:
        return "canary_settlement_not_live_eligible"
    if not plan.execution_authoritative:
        return "canary_price2_not_authoritative"
    payload = snapshot_payload(plan)
    if payload is None or payload.get("accepted") is not True:
        return "canary_price2_not_accepted"
    skew = payload.get("skew_ms")
    max_skew = payload.get("max_skew_ms", MAX_SKEW_MS)
    if skew is None:
        return "canary_price2_skew_unknown"
    try:
        if int(skew) > int(max_skew if max_skew is not None else MAX_SKEW_MS):
            return "canary_price2_skew"
        if int(max_skew if max_skew is not None else MAX_SKEW_MS) > MAX_SKEW_MS:
            return "canary_price2_skew"
    except (TypeError, ValueError):
        return "canary_price2_skew_unknown"
    age = payload.get("oldest_quote_age_ms")
    max_age = payload.get("max_quote_age_ms", MAX_QUOTE_AGE_MS)
    if age is None:
        return "canary_price2_stale"
    try:
        if int(age) > int(max_age if max_age is not None else MAX_QUOTE_AGE_MS):
            return "canary_price2_stale"
        if int(max_age if max_age is not None else MAX_QUOTE_AGE_MS) > MAX_QUOTE_AGE_MS:
            return "canary_price2_stale"
    except (TypeError, ValueError):
        return "canary_price2_stale"
    net = payload.get("net_edge")
    try:
        if net is None or Decimal(str(net)) < MIN_NET_EDGE:
            return "canary_min_net_edge"
    except (InvalidOperation, ValueError):
        return "canary_min_net_edge"
    costs = payload.get("fee_snapshots") or payload.get("venue_costs")
    fx = payload.get("fx_rates")
    if costs in (None, []) or fx in (None, []):
        return "canary_fee_fx_unknown"
    legs = [leg for leg in plan.legs if leg.requested_stake > 0]
    venues = {leg.venue for leg in legs}
    if venues != CANARY_VENUES:
        return "canary_requires_matchbook_polymarket"
    gbp = plan_outlay_gbp(plan)
    if gbp is None:
        return "canary_outlay_unknown"
    kind = canary_kind(tranche_id)
    if kind == "opening" and gbp > OPENING_TRANCHE_GBP:
        return "canary_opening_cap"
    if gbp > SESSION_CAP_GBP:
        return "canary_session_cap"
    minima = _native_minima_gbp(payload)
    if minima is not None and minima > OPENING_TRANCHE_GBP and kind == "opening":
        return "canary_native_minimum_exceeds_budget"
    if minima is not None and minima > gbp:
        return "canary_native_minimum_exceeds_budget"
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
            # Polymarket minima are native shares/USDC. Fail closed if FX missing
            # from the same snapshot's fx_rates.
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
    tranche_id: str,
    facts: LiveCanaryTradingFacts | None = None,
) -> str | None:
    """Refuse a live canary submit. None means budget reservation may proceed.

    ``live_execution_ready`` is intentionally ignored.
    """

    if not live_canary_enabled(settings):
        return None
    resolved = facts if facts is not None else resolve_trading_facts(settings)
    reasons = trading_permission_reasons(settings, resolved)
    if reasons:
        return reasons[0]
    return canary_plan_refusal(plan, tranche_id=tranche_id)


def public_canary_status(settings: Settings) -> dict[str, Any]:
    session_id = live_canary_session_id(settings)
    facts = _FACTS
    permission_reasons = trading_permission_reasons(
        settings, facts, require_facts=facts is not None
    )
    snapshot = get_live_canary_store(settings).session_snapshot(session_id)
    if snapshot.get("halt_new_orders"):
        permission_reasons.append("canary_halt_new_orders")
    if snapshot.get("blocked_reason"):
        permission_reasons.append(str(snapshot["blocked_reason"]))
    runtime_on = live_runtime_armed()
    ready = not permission_reasons and live_canary_enabled(settings)
    return {
        "enabled": live_canary_enabled(settings),
        "runtime_armed": runtime_on,
        "execution_toggle": "ON" if runtime_on else "OFF",
        "operator_confirmed": bool(
            settings.sports_hedge_live_canary_operator_confirmed or runtime_on
        ),
        "authorization_valid": authorization_valid(settings),
        "session_id": session_id,
        "capture_active": live_canary_capture_active(settings),
        "halt_new_orders": (not runtime_on) or bool(snapshot.get("halt_new_orders")),
        "authorized_opportunity_id": snapshot.get("authorized_opportunity_id"),
        "reserved_gbp": snapshot.get("reserved_gbp", "0"),
        "opening_tranche_gbp": format(OPENING_TRANCHE_GBP, "f"),
        "session_cap_gbp": format(SESSION_CAP_GBP, "f"),
        "max_opportunities": MAX_OPPORTUNITIES,
        "min_net_edge": format(MIN_NET_EDGE, "f"),
        "trading_permission": ready,
        "not_ready_reasons": permission_reasons,
        "live_execution_ready_is_not_permission": True,
        "paper_autofill_enabled": settings.paper_autofill_enabled,
        "disarm_does_not_cancel_existing": True,
        "preflight_at": _RUNTIME_PREFLIGHT_AT,
        "label": "EXECUTION ON" if runtime_on else "EXECUTION OFF",
        "reservations": snapshot.get("reservations", []),
        "blocked_reason": snapshot.get("blocked_reason"),
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
    _RUNTIME_PREFLIGHT_AT = datetime.now(UTC).isoformat()
    reasons = trading_permission_reasons(settings, facts, for_arm=True)
    if reasons:
        set_live_runtime_arm(False)
        status = public_canary_status(settings)
        status["armed"] = False
        status["not_ready_reasons"] = reasons
        status["preflight_at"] = _RUNTIME_PREFLIGHT_AT
        return status
    set_live_runtime_arm(True, session_id=live_canary_session_id(settings))
    get_live_canary_store(settings).ensure_session(live_canary_session_id(settings))
    status = public_canary_status(settings)
    status["armed"] = True
    return status


def disarm_live_runtime(settings: Settings) -> dict[str, Any]:
    """Stop new submits. Does not cancel orders already sent to a venue."""

    set_live_runtime_arm(False)
    return public_canary_status(settings)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
