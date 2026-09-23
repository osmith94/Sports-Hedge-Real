"""Versioned venue-cost registry shared by Arbitrage and Research."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
    require_aware_utc,
)

if TYPE_CHECKING:
    from sports_hedge.persistence.matchbook_account_fee import (
        MatchbookAccountFeeStatus,
        SqliteMatchbookAccountFeeStore,
    )

# Venue/account commission. Not a sport schedule. The historical name remains
# as an alias so existing imports keep resolving the same provider default.
MATCHBOOK_PROVIDER_DEFAULT_COMMISSION = Decimal("0.02")
MATCHBOOK_STANDARD_FOOTBALL_COMMISSION = MATCHBOOK_PROVIDER_DEFAULT_COMMISSION
MATCHBOOK_VENUE_COMMISSION_CLASS = "venue_commission"
MATCHBOOK_OVERRIDE_SOURCE = "operator_account_assumption:matchbook_net_win_commission"
MATCHBOOK_OVERRIDE_TIER = "operator_account_override"
MATCHBOOK_REGISTRY_SOURCE = "venue_cost_registry:matchbook_commission_schedule"


class UnknownRequiredCostError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class VenueCostRule(BaseModel):
    """Durable registry row. Snapshots are minted at resolve time."""

    venue: VenueName
    market_class: str
    action: MarketAction
    order_role: OrderRole = OrderRole.TAKER
    account_or_fee_tier: str = "standard"
    fee_basis: FeeBasis
    known_status: CostKnownStatus
    rate: Decimal | None = Field(default=None, ge=0, lt=1)
    fixed_amount: Decimal | None = Field(default=None, ge=0)
    formula_parameters: dict[str, Decimal] = Field(default_factory=dict)
    formula_name: str | None = None
    currency: str = "GBP"
    fee_scope: FeeScope = FeeScope.PER_QUOTE
    source: str
    effective_from: datetime
    catalog_version: str
    detail: str | None = None
    snapshot_id_prefix: str = "venue_cost"

    @model_validator(mode="after")
    def validate_rule(self) -> VenueCostRule:
        require_aware_utc(self.effective_from, "effective_from")
        self.market_class = self.market_class.strip().lower()
        if self.known_status is CostKnownStatus.UNKNOWN:
            return self
        if self.fee_basis is FeeBasis.UNKNOWN:
            raise ValueError("UNKNOWN fee basis cannot be marked known")
        return self

    def matches(
        self,
        *,
        venue: VenueName,
        market_class: str,
        action: MarketAction,
        order_role: OrderRole,
        account_or_fee_tier: str,
        as_of: datetime,
    ) -> bool:
        if as_of < self.effective_from:
            return False
        return (
            self.venue is venue
            and self.market_class == market_class.strip().lower()
            and self.action is action
            and self.order_role is order_role
            and self.account_or_fee_tier == account_or_fee_tier
        )

    def to_snapshot(
        self,
        *,
        captured_at: datetime,
        market_class: str | None = None,
    ) -> VenueCostSnapshot:
        priced_class = (market_class or self.market_class).strip().lower()
        return VenueCostSnapshot(
            venue=self.venue,
            action=self.action,
            fee_basis=self.fee_basis,
            known_status=self.known_status,
            captured_at=captured_at,
            source=self.source,
            market_class=priced_class,
            order_role=self.order_role,
            fee_scope=self.fee_scope,
            account_or_fee_tier=self.account_or_fee_tier,
            rate=self.rate,
            fixed_amount=self.fixed_amount,
            formula_parameters=dict(self.formula_parameters),
            formula_name=self.formula_name,
            currency=self.currency,
            effective_from=self.effective_from,
            snapshot_id=(
                f"{self.snapshot_id_prefix}:{self.catalog_version}:{self.venue.value}:"
                f"{priced_class}:{self.action.value}:{self.order_role.value}"
            ),
            detail=self.detail,
        )


class VenueCostResolver:
    """Provider-neutral cost lookup. Unknown required costs fail closed."""

    def __init__(
        self,
        rules: list[VenueCostRule] | None = None,
        *,
        matchbook_fee_store: SqliteMatchbookAccountFeeStore | None = None,
    ) -> None:
        self.rules = list(rules or phase1_seed_rules())
        self.matchbook_fee_store = matchbook_fee_store

    def resolve(
        self,
        *,
        venue: VenueName,
        market_class: str | MarketFamily,
        action: MarketAction,
        as_of: datetime,
        order_role: OrderRole = OrderRole.TAKER,
        account_or_fee_tier: str = "standard",
    ) -> VenueCostSnapshot:
        require_aware_utc(as_of, "as_of")
        raw_family = market_class.value if isinstance(market_class, MarketFamily) else market_class
        family = raw_family.strip().lower()
        if family in {"", "unknown"}:
            raise UnknownRequiredCostError(
                f"unknown_required_venue_cost:{venue.value}",
                f"market class unknown; {venue.value} cost cannot be assumed",
            )
        if venue is VenueName.POLYMARKET:
            raise UnknownRequiredCostError(
                f"unknown_required_venue_cost:{venue.value}:{family}",
                (
                    "Polymarket costs resolve from per-market CLOB/Gamma fee metadata; "
                    "the venue-cost registry does not seed a global zero"
                ),
            )
        chosen = self._select_rule(
            venue=venue,
            market_class=family,
            action=action,
            order_role=order_role,
            account_or_fee_tier=account_or_fee_tier,
            as_of=as_of,
        )
        if chosen is None:
            raise UnknownRequiredCostError(
                f"unknown_required_venue_cost:{venue.value}:{family}",
                (
                    f"no venue-cost rule for {venue.value} {family} {action.value} "
                    f"{order_role.value} {account_or_fee_tier}"
                ),
            )
        snapshot = chosen.to_snapshot(captured_at=as_of, market_class=family)
        if (
            chosen.market_class == MATCHBOOK_VENUE_COMMISSION_CLASS
            and family != MATCHBOOK_VENUE_COMMISSION_CLASS
        ):
            snapshot = snapshot.model_copy(
                update={
                    "detail": (
                        f"{snapshot.detail} Applied to market class {family} from the "
                        "Matchbook venue/account commission schedule."
                    )
                }
            )
        snapshot = self._apply_matchbook_override(snapshot)
        if snapshot.known_status is CostKnownStatus.UNKNOWN:
            raise UnknownRequiredCostError(
                f"unknown_required_venue_cost:{venue.value}:{family}",
                snapshot.detail or "required venue cost is unknown",
            )
        return snapshot

    def _select_rule(
        self,
        *,
        venue: VenueName,
        market_class: str,
        action: MarketAction,
        order_role: OrderRole,
        account_or_fee_tier: str,
        as_of: datetime,
    ) -> VenueCostRule | None:
        """Exact market-class rules beat the Matchbook venue/account schedule.

        The fallback is Matchbook-only. Kalshi series/event metadata and
        Polymarket per-market metadata stay outside this registry, and a
        missing rule for any other venue still fails closed.
        """

        exact = self._matching_rules(
            venue=venue,
            market_class=market_class,
            action=action,
            order_role=order_role,
            account_or_fee_tier=account_or_fee_tier,
            as_of=as_of,
        )
        if exact:
            return exact[0]
        if venue is not VenueName.MATCHBOOK or market_class == MATCHBOOK_VENUE_COMMISSION_CLASS:
            return None
        defaults = self._matching_rules(
            venue=venue,
            market_class=MATCHBOOK_VENUE_COMMISSION_CLASS,
            action=action,
            order_role=order_role,
            account_or_fee_tier=account_or_fee_tier,
            as_of=as_of,
        )
        return defaults[0] if defaults else None

    def _matching_rules(
        self,
        *,
        venue: VenueName,
        market_class: str,
        action: MarketAction,
        order_role: OrderRole,
        account_or_fee_tier: str,
        as_of: datetime,
    ) -> list[VenueCostRule]:
        matches = [
            rule
            for rule in self.rules
            if rule.matches(
                venue=venue,
                market_class=market_class,
                action=action,
                order_role=order_role,
                account_or_fee_tier=account_or_fee_tier,
                as_of=as_of,
            )
        ]
        matches.sort(key=lambda rule: rule.effective_from, reverse=True)
        return matches

    def list_status(self, *, as_of: datetime) -> list[VenueCostSnapshot]:
        require_aware_utc(as_of, "as_of")
        latest: dict[tuple, VenueCostRule] = {}
        for rule in self.rules:
            if as_of < rule.effective_from:
                continue
            key = (
                rule.venue,
                rule.market_class,
                rule.action,
                rule.order_role,
                rule.account_or_fee_tier,
            )
            current = latest.get(key)
            if current is None or rule.effective_from > current.effective_from:
                latest[key] = rule
        return [
            self._apply_matchbook_override(rule.to_snapshot(captured_at=as_of))
            for rule in latest.values()
        ]

    def matchbook_account_status(self, *, as_of: datetime) -> MatchbookAccountFeeStatus:
        from sports_hedge.fees.labels import operator_fee_label
        from sports_hedge.persistence.matchbook_account_fee import MatchbookAccountFeeStatus

        override = (
            self.matchbook_fee_store.get_override()
            if self.matchbook_fee_store is not None
            else None
        )
        override_rate = override[0] if override is not None else None
        updated_at = override[1] if override is not None else None
        effective = (
            override_rate if override_rate is not None else MATCHBOOK_PROVIDER_DEFAULT_COMMISSION
        )
        snapshot = self.resolve(
            venue=VenueName.MATCHBOOK,
            market_class=MarketFamily.BOTH_TEAMS_TO_SCORE,
            action=MarketAction.BACK,
            as_of=as_of,
        )
        return MatchbookAccountFeeStatus(
            provider_default_rate=MATCHBOOK_PROVIDER_DEFAULT_COMMISSION,
            override_rate=override_rate,
            effective_rate=effective,
            fee_basis=FeeBasis.PROFIT_COMMISSION,
            account_assumption=override_rate is not None,
            label=operator_fee_label(snapshot),
            source=snapshot.source,
            detail=snapshot.detail or "",
            updated_at=updated_at,
        )

    def _apply_matchbook_override(self, snapshot: VenueCostSnapshot) -> VenueCostSnapshot:
        if snapshot.venue is not VenueName.MATCHBOOK:
            return snapshot
        if snapshot.fee_basis is not FeeBasis.PROFIT_COMMISSION:
            return snapshot
        if self.matchbook_fee_store is None:
            return snapshot
        override = self.matchbook_fee_store.get_override()
        if override is None:
            return snapshot
        rate, updated_at = override
        return snapshot.model_copy(
            update={
                "rate": rate,
                "source": MATCHBOOK_OVERRIDE_SOURCE,
                "account_or_fee_tier": MATCHBOOK_OVERRIDE_TIER,
                "snapshot_id": f"{snapshot.snapshot_id}:operator_override",
                "detail": (
                    "Operator/account Matchbook net-profit commission "
                    f"{rate} replacing provider default {MATCHBOOK_PROVIDER_DEFAULT_COMMISSION}; "
                    f"saved {updated_at.isoformat()}."
                ),
            }
        )


def phase1_seed_rules() -> list[VenueCostRule]:
    """Backend-owned Phase 1 paper catalog. Not a generic operator settings surface.

    Matchbook commission is one venue/account net-win schedule. It applies to
    every known market class, including sports added later, unless an explicit
    market-class rule records different authoritative economics. Polymarket is
    intentionally absent: per-market CLOB/Gamma fee metadata is required.
    Kalshi is absent: series/event fee metadata is required. Unknown market
    classes and venues without a rule fail closed. Nothing here is a zero fee.
    """

    effective = datetime(2024, 1, 1, tzinfo=UTC)
    version = "phase1-2026-09-22-venue-commission"
    detail = (
        "Matchbook taker net-win commission from the venue/account schedule; "
        "provider default until an operator/account override is saved."
    )
    lay_detail = (
        "Matchbook taker net-win commission for closing lays from the venue/account "
        "schedule; provider default until an operator/account override is saved."
    )
    return [
        _matchbook_commission_rule(
            action=MarketAction.BACK,
            effective=effective,
            version=version,
            detail=detail,
        ),
        _matchbook_commission_rule(
            action=MarketAction.LAY,
            effective=effective,
            version=version,
            detail=lay_detail,
        ),
    ]


def _matchbook_commission_rule(
    *,
    action: MarketAction,
    effective: datetime,
    version: str,
    detail: str,
) -> VenueCostRule:
    return VenueCostRule(
        venue=VenueName.MATCHBOOK,
        market_class=MATCHBOOK_VENUE_COMMISSION_CLASS,
        action=action,
        order_role=OrderRole.TAKER,
        fee_basis=FeeBasis.PROFIT_COMMISSION,
        known_status=CostKnownStatus.KNOWN,
        rate=MATCHBOOK_PROVIDER_DEFAULT_COMMISSION,
        currency="GBP",
        source=MATCHBOOK_REGISTRY_SOURCE,
        effective_from=effective,
        catalog_version=version,
        detail=detail,
    )
