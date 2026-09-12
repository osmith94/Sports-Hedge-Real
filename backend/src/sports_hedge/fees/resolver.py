"""Versioned venue-cost registry shared by Arbitrage and Research."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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

    def to_snapshot(self, *, captured_at: datetime) -> VenueCostSnapshot:
        return VenueCostSnapshot(
            venue=self.venue,
            action=self.action,
            fee_basis=self.fee_basis,
            known_status=self.known_status,
            captured_at=captured_at,
            source=self.source,
            market_class=self.market_class,
            order_role=self.order_role,
            fee_scope=self.fee_scope,
            account_or_fee_tier=self.account_or_fee_tier,
            rate=self.rate,
            fixed_amount=self.fixed_amount,
            currency=self.currency,
            effective_from=self.effective_from,
            snapshot_id=f"{self.snapshot_id_prefix}:{self.catalog_version}:{self.venue.value}:{self.market_class}:{self.action.value}:{self.order_role.value}",
            detail=self.detail,
        )


class VenueCostResolver:
    """Provider-neutral cost lookup. Unknown required costs fail closed."""

    def __init__(self, rules: list[VenueCostRule] | None = None) -> None:
        self.rules = list(rules or phase1_seed_rules())

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
        family = market_class.value if isinstance(market_class, MarketFamily) else market_class
        if family.strip().lower() in {"", "unknown"}:
            raise UnknownRequiredCostError(
                f"unknown_required_venue_cost:{venue.value}",
                f"market class unknown; {venue.value} cost cannot be assumed",
            )
        matches = [
            rule
            for rule in self.rules
            if rule.matches(
                venue=venue,
                market_class=family,
                action=action,
                order_role=order_role,
                account_or_fee_tier=account_or_fee_tier,
                as_of=as_of,
            )
        ]
        if not matches:
            raise UnknownRequiredCostError(
                f"unknown_required_venue_cost:{venue.value}:{family}",
                (
                    f"no venue-cost rule for {venue.value} {family} {action.value} "
                    f"{order_role.value} {account_or_fee_tier}"
                ),
            )
        matches.sort(key=lambda rule: rule.effective_from, reverse=True)
        snapshot = matches[0].to_snapshot(captured_at=as_of)
        if snapshot.known_status is CostKnownStatus.UNKNOWN:
            raise UnknownRequiredCostError(
                f"unknown_required_venue_cost:{venue.value}:{family}",
                snapshot.detail or "required venue cost is unknown",
            )
        return snapshot

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
        return [rule.to_snapshot(captured_at=as_of) for rule in latest.values()]


def phase1_seed_rules() -> list[VenueCostRule]:
    """Backend-owned Phase 1 paper catalog. Not operator-editable.

    Football exchange commission is modelled as per-quote profit commission and
    is not assumed identical across market classes. Polymarket public CLOB
    sports are seeded as none_confirmed (explicit known zero-formula), not as
    an operator 0% dashboard assumption. Unknown classes remain absent.
    """

    effective = datetime(2024, 1, 1, tzinfo=UTC)
    version = "phase1-2026-09-12"
    football = [
        family
        for family in MarketFamily
        if family not in {MarketFamily.UNKNOWN, MarketFamily.PLAYER_PROPS}
    ]
    rules: list[VenueCostRule] = []
    for family in football:
        rules.append(
            VenueCostRule(
                venue=VenueName.MATCHBOOK,
                market_class=family.value,
                action=MarketAction.BACK,
                order_role=OrderRole.TAKER,
                fee_basis=FeeBasis.PROFIT_COMMISSION,
                known_status=CostKnownStatus.KNOWN,
                rate=Decimal("0.02"),
                currency="GBP",
                source="venue_cost_registry:matchbook_commission_schedule",
                effective_from=effective,
                catalog_version=version,
                detail="Phase 1 seeded Matchbook football taker profit commission; not a live account-tier lookup.",
            )
        )
        rules.append(
            VenueCostRule(
                venue=VenueName.MATCHBOOK,
                market_class=family.value,
                action=MarketAction.LAY,
                order_role=OrderRole.TAKER,
                fee_basis=FeeBasis.PROFIT_COMMISSION,
                known_status=CostKnownStatus.KNOWN,
                rate=Decimal("0.02"),
                currency="GBP",
                source="venue_cost_registry:matchbook_commission_schedule",
                effective_from=effective,
                catalog_version=version,
                detail="Phase 1 seeded Matchbook football taker profit commission for closing lays only.",
            )
        )
        rules.append(
            VenueCostRule(
                venue=VenueName.POLYMARKET,
                market_class=family.value,
                action=MarketAction.BUY,
                order_role=OrderRole.TAKER,
                fee_basis=FeeBasis.NONE_CONFIRMED,
                known_status=CostKnownStatus.KNOWN,
                currency="USD",
                source="venue_cost_registry:polymarket_fee_schedule",
                effective_from=effective,
                catalog_version=version,
                detail="Phase 1 seeded Polymarket sports CLOB none_confirmed for listed football families.",
            )
        )
        rules.append(
            VenueCostRule(
                venue=VenueName.POLYMARKET,
                market_class=family.value,
                action=MarketAction.SELL,
                order_role=OrderRole.TAKER,
                fee_basis=FeeBasis.NONE_CONFIRMED,
                known_status=CostKnownStatus.KNOWN,
                currency="USD",
                source="venue_cost_registry:polymarket_fee_schedule",
                effective_from=effective,
                catalog_version=version,
                detail="Phase 1 seeded Polymarket sports CLOB none_confirmed for closing sells.",
            )
        )
    rules.append(
        VenueCostRule(
            venue=VenueName.MATCHBOOK,
            market_class=MarketFamily.PLAYER_PROPS.value,
            action=MarketAction.BACK,
            order_role=OrderRole.TAKER,
            fee_basis=FeeBasis.PROFIT_COMMISSION,
            known_status=CostKnownStatus.KNOWN,
            rate=Decimal("0.05"),
            currency="GBP",
            source="venue_cost_registry:matchbook_commission_schedule",
            effective_from=effective,
            catalog_version=version,
            detail="Phase 1 seeded Matchbook player-prop taker commission differs from football match markets.",
        )
    )
    rules.append(
        VenueCostRule(
            venue=VenueName.POLYMARKET,
            market_class=MarketFamily.PLAYER_PROPS.value,
            action=MarketAction.BUY,
            order_role=OrderRole.TAKER,
            fee_basis=FeeBasis.PAYOUT,
            known_status=CostKnownStatus.KNOWN,
            rate=Decimal("0.02"),
            currency="USD",
            source="venue_cost_registry:polymarket_fee_schedule",
            effective_from=effective,
            catalog_version=version,
            detail="Phase 1 seeded Polymarket player-prop payout fee; different basis from none_confirmed sports.",
        )
    )
    return rules
