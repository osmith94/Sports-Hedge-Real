from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.models import VenueName


def require_aware_utc(value: datetime, field_name: str) -> datetime:
    """Reject naive or non-UTC datetimes. Never guess UTC at this boundary."""

    if value.tzinfo is None:
        raise ValueError(
            f"{field_name} must be timezone-aware UTC; naive datetimes are rejected"
        )
    offset = value.utcoffset()
    if offset is None or offset != timedelta(0):
        raise ValueError(
            f"{field_name} must be UTC; non-UTC offsets are rejected rather than converted"
        )
    return value


class FeeBasis(StrEnum):
    """How a venue charges for a specific market, side and order role.

    These are economic bases, not venue names. Unknown or unsupported bases
    must fail closed rather than being coerced into profit commission.
    """

    PROFIT_COMMISSION = "profit_commission"
    STAKE_OR_NOTIONAL = "stake_or_notional"
    PAYOUT = "payout"
    TRANSACTION = "transaction"
    FIXED = "fixed"
    FORMULA = "formula"
    NONE_CONFIRMED = "none_confirmed"
    UNKNOWN = "unknown"


class CostKnownStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class OrderRole(StrEnum):
    MAKER = "maker"
    TAKER = "taker"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class FeeScope(StrEnum):
    """How a fee is assessed economically.

    This per-quote engine only prices ``PER_QUOTE`` legs. Market-net P&L,
    account-period, and netted-commission schemes require stateful accounting
    and must fail closed here rather than being approximated.
    """

    PER_QUOTE = "per_quote"
    MARKET_NET_PNL = "market_net_pnl"
    ACCOUNT_PERIOD = "account_period"
    NETTED_COMMISSION = "netted_commission"
    UNKNOWN = "unknown"


class MarketAction(StrEnum):
    BACK = "back"
    BUY = "buy"
    LAY = "lay"
    SELL = "sell"


class VenueCostSnapshot(BaseModel):
    """Provider-neutral fee/cost assumption for one venue market action.

    This is the shared effective-economics contract for Research and, later,
    Arbitrage. It stores provenance and the fee basis; it does not place orders.
    """

    venue: VenueName
    action: MarketAction
    fee_basis: FeeBasis
    known_status: CostKnownStatus
    captured_at: datetime
    source: str
    source_market_id: str | None = None
    market_class: str | None = None
    order_role: OrderRole = OrderRole.NOT_APPLICABLE
    fee_scope: FeeScope = FeeScope.PER_QUOTE
    account_or_fee_tier: str | None = None
    rate: Decimal | None = Field(default=None, ge=0, lt=1)
    fixed_amount: Decimal | None = Field(default=None, ge=0)
    formula_parameters: dict[str, Decimal] = Field(default_factory=dict)
    currency: str = "GBP"
    effective_from: datetime | None = None
    snapshot_id: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> VenueCostSnapshot:
        require_aware_utc(self.captured_at, "captured_at")
        if self.effective_from is not None:
            require_aware_utc(self.effective_from, "effective_from")
        if self.known_status is CostKnownStatus.UNKNOWN:
            return self
        if self.fee_basis is FeeBasis.UNKNOWN:
            raise ValueError("UNKNOWN fee basis cannot be marked known")
        if self.fee_basis is FeeBasis.NONE_CONFIRMED:
            return self
        if self.fee_basis is FeeBasis.FIXED and self.fixed_amount is None:
            raise ValueError("FIXED fee basis requires fixed_amount when costs are known")
        if self.fee_basis in {
            FeeBasis.PROFIT_COMMISSION,
            FeeBasis.STAKE_OR_NOTIONAL,
            FeeBasis.PAYOUT,
            FeeBasis.TRANSACTION,
        } and self.rate is None:
            raise ValueError(f"{self.fee_basis} requires rate when costs are known")
        return self

    def is_economically_known(self) -> bool:
        return (
            self.known_status is CostKnownStatus.KNOWN
            and self.fee_basis is not FeeBasis.UNKNOWN
        )
