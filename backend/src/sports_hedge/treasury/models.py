"""Paper treasury identities and operator-visible state.

Native `(venue, currency)` pools are independent. GBP carrying values are
derived presentation data and are never summed across currencies.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.domain.models import VenueName


class PaperTreasuryEventType(StrEnum):
    SESSION_OPEN = "session_open"
    SESSION_CLOSE = "session_close"
    SEED = "seed"
    LOCK = "lock"
    RELEASE = "release"
    REALISED_PNL = "realised_pnl"
    FEE = "fee"
    CORRECTION = "correction"
    FX_CARRYING_SNAPSHOT = "fx_carrying_snapshot"


class PaperTreasurySession(BaseModel):
    session_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    active: bool
    provenance: DataProvenance = DataProvenance.LIVE_PAPER
    reason: str
    seed_gbp: Decimal
    fx_rate_usd_gbp: Decimal
    fx_source: str
    fx_as_of: datetime
    include_kalshi: bool = True
    capital_kind: str = "paper_hypothetical"


class PaperTreasuryPoolState(BaseModel):
    pool_id: str
    session_id: str
    venue: VenueName
    native_currency: str
    identity: str
    seed_native: Decimal = Field(ge=0)
    available_cash: Decimal = Field(ge=0)
    locked_capital: Decimal = Field(ge=0)
    realised_pnl_native: Decimal
    cumulative_fees_native: Decimal = Field(ge=0)
    gbp_carrying_value: Decimal | None = None
    gbp_carrying_status: str = "fx_unavailable"
    fx_rate_gbp_per_unit: Decimal | None = None
    fx_source: str | None = None
    fx_as_of: datetime | None = None

    @model_validator(mode="after")
    def native_buckets_are_consistent(self) -> PaperTreasuryPoolState:
        self.native_currency = self.native_currency.upper()
        if self.available_cash < 0 or self.locked_capital < 0:
            raise ValueError("treasury native buckets cannot be negative")
        return self

    @property
    def native_total(self) -> Decimal:
        return self.available_cash + self.locked_capital


class PaperTreasuryEvent(BaseModel):
    event_id: str
    session_id: str
    pool_id: str
    venue: VenueName
    native_currency: str
    event_type: PaperTreasuryEventType
    native_amount: Decimal
    occurred_at: datetime
    trade_id: str | None = None
    opportunity_id: str | None = None
    lock_id: str | None = None
    source: str
    source_id: str
    reason: str
    fx_rate_gbp_per_unit: Decimal | None = None
    fx_source: str | None = None
    journal_id: str | None = None


class PaperTreasurySnapshot(BaseModel):
    data_kind: str = "live_paper"
    capital_kind: str = "paper_hypothetical"
    execution_enabled: bool = False
    mode: str = "paper"
    session: PaperTreasurySession | None = None
    pools: list[PaperTreasuryPoolState] = Field(default_factory=list)
    events: list[PaperTreasuryEvent] = Field(default_factory=list)
    note: str = (
        "PAPER MODE. Native venue pools are hypothetical paper capital, not live venue funds. "
        "USD and GBP are never summed. conditionally_releasable amounts are not spendable cash."
    )

    def pool(self, venue: VenueName, currency: str | None = None) -> PaperTreasuryPoolState:
        currency_u = currency.upper() if currency else None
        matches = [
            item
            for item in self.pools
            if item.venue is venue and (currency_u is None or item.native_currency == currency_u)
        ]
        if len(matches) != 1:
            raise KeyError(f"{venue.value}/{currency_u}")
        return matches[0]

    def combined_cash_gbp(self) -> Decimal:
        raise ValueError("USD and GBP treasury pools must not be summed as one cash figure")


class TreasuryLockRequest(BaseModel):
    venue: VenueName
    native_currency: str
    amount_native: Decimal = Field(gt=0)
    lock_id: str = Field(min_length=1)
    trade_id: str | None = None
    opportunity_id: str | None = None
    source: str = "paper_fill_simulator"
    reason: str = "PAPER-ONLY capital lock on validated paper fill"
    fx_rate_gbp_per_unit: Decimal = Field(gt=0)
    capital_source: str = "AUTO_POOL"

    @model_validator(mode="after")
    def uppercase_currency(self) -> TreasuryLockRequest:
        self.native_currency = self.native_currency.upper()
        return self


class UnwindReleaseLeg(BaseModel):
    venue: VenueName
    native_currency: str
    lock_id: str
    amount_native: Decimal = Field(gt=0)
    realised_pnl_native: Decimal = Decimal("0")
    fee_native: Decimal = Field(default=Decimal("0"), ge=0)
    fx_rate_gbp_per_unit: Decimal = Field(gt=0)


class ValidatedUnwindResult(BaseModel):
    """Step 8D supplies this after a paper close is accepted as completed.

    `conditionally_releasable_*` is analytical metadata and never posts.
    """

    trade_id: str
    close_completed: bool = False
    opportunity_id: str | None = None
    source: str = "paper_unwind"
    source_id: str | None = None
    reason: str = "validated paper unwind"
    conditionally_releasable_native: dict[str, Decimal] = Field(default_factory=dict)
    releases: list[UnwindReleaseLeg] = Field(default_factory=list)
