"""Live venue spendable capital for a Real Price-2 acceptance.

Paper mode does not call this. Real mode reads each required account once,
before ``accepted`` is returned, and stores the evidence on the snapshot.
Execution does not read it again. A missing or short balance is a rejection.
Paper treasury is not a substitute.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_hedge.application.execution_snapshot import FrozenNativeOrder, VenueReadinessEvidence
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.polymarket_buy_readiness import read_collateral_evidence

VENUE_CAPITAL_UNPROVEN = "venue_capital_unproven"
VENUE_CAPITAL_INSUFFICIENT = "venue_capital_insufficient"
POLYMARKET_CONSTRAINTS_UNPROVEN = "polymarket_native_constraints_unproven"
MATCHBOOK_ORDER_UNPROVEN = "matchbook_native_order_unproven"

_REAL_VENUES = frozenset({VenueName.POLYMARKET.value, VenueName.MATCHBOOK.value})
POLYMARKET_COLLATERAL_SOURCE = "polymarket_collateral"
MATCHBOOK_FREE_FUNDS_SOURCE = "matchbook_free_funds"


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def judge_real_package(
    *,
    mode: str,
    legs: Sequence[Any],
    frozen_orders: Sequence[FrozenNativeOrder],
    readiness: Sequence[VenueReadinessEvidence],
) -> tuple[str | None, tuple[VenueReadinessEvidence, ...]]:
    """Reject a real package whose native order or live capital is unproven.

    Paper mode returns no block and leaves the readiness rows unchanged.
    Comparison is local. This function does not perform network I/O.
    """

    if mode != "real":
        return None, tuple(readiness)
    frozen_by_leg = {
        (item.venue, item.native_runner_id): item for item in frozen_orders
    }
    readiness_by_venue = {item.venue: item for item in readiness}
    required: dict[str, Decimal] = {}
    for leg in legs:
        stake = getattr(leg, "requested_stake", None)
        if stake is None or stake <= 0:
            continue
        venue = getattr(leg.venue, "value", leg.venue)
        venue_name = str(venue)
        if venue_name not in _REAL_VENUES:
            continue
        runner = str(getattr(leg, "source_runner_id", "") or "").strip()
        frozen = frozen_by_leg.get((venue_name, runner))
        if frozen is None:
            reason = (
                POLYMARKET_CONSTRAINTS_UNPROVEN
                if venue_name == VenueName.POLYMARKET.value
                else MATCHBOOK_ORDER_UNPROVEN
            )
            return reason, _with_required(readiness_by_venue, required)
        native = frozen.native_amount if venue_name == VenueName.POLYMARKET.value else frozen.native_stake
        amount = _decimal(native)
        if amount is None or amount <= 0:
            reason = (
                POLYMARKET_CONSTRAINTS_UNPROVEN
                if venue_name == VenueName.POLYMARKET.value
                else MATCHBOOK_ORDER_UNPROVEN
            )
            return reason, _with_required(readiness_by_venue, required)
        required[venue_name] = required.get(venue_name, Decimal(0)) + amount
    for venue_name, needed in required.items():
        evidence = readiness_by_venue.get(venue_name)
        if evidence is None or not evidence.proven:
            return VENUE_CAPITAL_UNPROVEN, _with_required(readiness_by_venue, required)
        spendable = _decimal(evidence.spendable)
        if spendable is None:
            return VENUE_CAPITAL_UNPROVEN, _with_required(readiness_by_venue, required)
        if spendable < needed:
            return VENUE_CAPITAL_INSUFFICIENT, _with_required(readiness_by_venue, required)
        if venue_name == VenueName.POLYMARKET.value:
            allowance = _decimal(evidence.allowance)
            if allowance is None:
                return VENUE_CAPITAL_UNPROVEN, _with_required(readiness_by_venue, required)
            if allowance < needed:
                return VENUE_CAPITAL_INSUFFICIENT, _with_required(readiness_by_venue, required)
    return None, _with_required(readiness_by_venue, required)


def _with_required(
    readiness_by_venue: dict[str, VenueReadinessEvidence],
    required: dict[str, Decimal],
) -> tuple[VenueReadinessEvidence, ...]:
    rows: list[VenueReadinessEvidence] = []
    for venue_name, evidence in readiness_by_venue.items():
        needed = required.get(venue_name)
        rows.append(
            VenueReadinessEvidence(
                venue=evidence.venue,
                currency=evidence.currency,
                proven=evidence.proven,
                spendable=evidence.spendable,
                allowance=evidence.allowance,
                required_stake=evidence.required_stake if needed is None else format(needed, "f"),
                source=evidence.source,
                reason=evidence.reason,
            )
        )
    return tuple(rows)


class LiveVenueCapitalAuthority:
    """One account read per venue. Not an order submission."""

    def __init__(
        self,
        settings: Settings,
        *,
        polymarket_client_factory: Any = None,
        matchbook_reader: Any = None,
    ) -> None:
        self._settings = settings
        self._polymarket_client_factory = polymarket_client_factory
        self._matchbook_reader = matchbook_reader

    async def read(self, venues: Sequence[Any]) -> tuple[VenueReadinessEvidence, ...]:
        names = {getattr(venue, "value", venue) for venue in venues}
        rows: list[VenueReadinessEvidence] = []
        if VenueName.POLYMARKET.value in names or VenueName.POLYMARKET in names:
            rows.append(await asyncio.to_thread(self._polymarket))
        if VenueName.MATCHBOOK.value in names or VenueName.MATCHBOOK in names:
            rows.append(await self._matchbook())
        return tuple(rows)

    def _polymarket(self) -> VenueReadinessEvidence:
        client = None
        try:
            if self._polymarket_client_factory is not None:
                client = self._polymarket_client_factory()
            else:
                from sports_hedge.execution.polymarket_sdk import open_polymarket_client

                client = open_polymarket_client(self._settings, derive_credentials=True)
            spendable, allowance, reason = read_collateral_evidence(client)
        except Exception as exc:  # noqa: BLE001 — unreadable capital rejects the package
            return _unproven(
                VenueName.POLYMARKET.value,
                "USD",
                POLYMARKET_COLLATERAL_SOURCE,
                type(exc).__name__,
            )
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()
        if spendable is None or allowance is None:
            return VenueReadinessEvidence(
                venue=VenueName.POLYMARKET.value,
                currency="USD",
                proven=False,
                spendable=_money(spendable),
                allowance=_money(allowance),
                required_stake=None,
                source=POLYMARKET_COLLATERAL_SOURCE,
                reason=reason or VENUE_CAPITAL_UNPROVEN,
            )
        return VenueReadinessEvidence(
            venue=VenueName.POLYMARKET.value,
            currency="USD",
            proven=True,
            spendable=_money(spendable),
            allowance=_money(allowance),
            required_stake=None,
            source=POLYMARKET_COLLATERAL_SOURCE,
            reason=None,
        )

    async def _matchbook(self) -> VenueReadinessEvidence:
        currency = self._settings.matchbook_currency
        try:
            if self._matchbook_reader is not None:
                snapshot = await self._matchbook_reader()
            else:
                from sports_hedge.execution.matchbook_http import MatchbookHttpExecutionTransport

                transport = MatchbookHttpExecutionTransport(self._settings)
                try:
                    snapshot = await transport.account_snapshot()
                finally:
                    await transport.aclose()
        except Exception as exc:  # noqa: BLE001 — unreadable capital rejects the package
            return _unproven(
                VenueName.MATCHBOOK.value,
                currency,
                MATCHBOOK_FREE_FUNDS_SOURCE,
                type(exc).__name__,
            )
        if not isinstance(snapshot, dict) or not snapshot.get("balance_readable"):
            detail = snapshot.get("authentication_detail") if isinstance(snapshot, dict) else None
            return _unproven(
                VenueName.MATCHBOOK.value,
                currency,
                MATCHBOOK_FREE_FUNDS_SOURCE,
                str(detail or "free-funds unread"),
            )
        if snapshot.get("currency_compatible") is False:
            return _unproven(
                VenueName.MATCHBOOK.value,
                currency,
                MATCHBOOK_FREE_FUNDS_SOURCE,
                "currency_conflict",
            )
        reported = snapshot.get("currency")
        return VenueReadinessEvidence(
            venue=VenueName.MATCHBOOK.value,
            currency=str(reported or currency),
            proven=True,
            spendable=str(snapshot.get("free_funds")),
            allowance=None,
            required_stake=None,
            source=MATCHBOOK_FREE_FUNDS_SOURCE,
            reason=None,
        )


def _unproven(venue: str, currency: str, source: str, reason: str) -> VenueReadinessEvidence:
    return VenueReadinessEvidence(
        venue=venue,
        currency=currency,
        proven=False,
        spendable=None,
        allowance=None,
        required_stake=None,
        source=source,
        reason=reason,
    )
