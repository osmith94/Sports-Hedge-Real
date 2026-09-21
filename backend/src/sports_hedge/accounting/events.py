"""CQRS accounting domain events.

Operational scanner/trading/Treasury writes emit these small immutable facts.
GL, balance-sheet, reconciliation and management-reporting projections are
rebuilt on demand from the event stream — never on the scan critical path.

Event schema v1 covers the #476 initial contract. Payload fields that a given
type does not use remain unset rather than inventing balances or FX.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from sports_hedge.accounting.dimensions import (
    AttributionScope,
    CapitalSource,
    CashState,
    StrategyBook,
)
from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.domain.models import VenueName

EVENT_SCHEMA_VERSION = 1


class AccountingEventType(StrEnum):
    CAPITAL_INTRODUCED = "capital_introduced"
    CAPITAL_WITHDRAWN = "capital_withdrawn"
    TREASURY_TRANSFER = "treasury_transfer"
    FX_CONVERSION = "fx_conversion"
    FX_REVALUATION = "fx_revaluation"
    PAPER_TRADE_OPENED = "paper_trade_opened"
    PAPER_TRADE_FILL = "paper_trade_fill"
    PAPER_CAPITAL_LOCK = "paper_capital_lock"
    PAPER_CAPITAL_RELEASE = "paper_capital_release"
    SETTLEMENT_PNL = "settlement_pnl"
    FEE_POSTED = "fee_posted"
    MANUAL_ADJUSTMENT = "manual_adjustment"


class DuplicateAccountingEventError(ValueError):
    """Raised when (source, source_id) or event_id is reused with conflicting facts."""


class AccountingEventPayload(BaseModel):
    """Small source facts for one domain event. Native amounts stay per-currency."""

    venue: VenueName | None = None
    currency: str | None = None
    amount_native: Decimal = Decimal("0")
    amount_gbp: Decimal = Decimal("0")
    fx_rate_gbp_per_unit: Decimal | None = Field(default=None, gt=0)
    fx_source: str | None = None
    fx_source_date: date | None = None
    fx_status: str | None = None
    capital_source: CapitalSource = CapitalSource.SHARED_UNALLOCATED
    attribution: AttributionScope = AttributionScope.SHARED_UNALLOCATED
    strategy_book: StrategyBook | None = None
    cash_state: CashState | None = None
    counterparty_venue: VenueName | None = None
    counterparty_currency: str | None = None
    counterparty_amount_native: Decimal | None = None
    counterparty_amount_gbp: Decimal | None = None
    trade_id: str | None = None
    opportunity_id: str | None = None
    lock_id: str | None = None
    session_id: str | None = None
    pool_id: str | None = None
    reason: str = ""
    adjustment_kind: str | None = None
    operator_reference: str | None = None
    fill_kind: str | None = None
    execution_mode: str | None = None
    journal_id: str | None = None

    @field_validator("currency", "counterparty_currency", mode="before")
    @classmethod
    def uppercase_currency(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return str(value).upper()

    @model_validator(mode="after")
    def attribution_matches_book(self) -> AccountingEventPayload:
        if self.attribution is AttributionScope.STRATEGY and self.strategy_book is None:
            raise ValueError("STRATEGY attribution requires a strategy_book")
        if self.attribution is AttributionScope.SHARED_UNALLOCATED and self.strategy_book is not None:
            raise ValueError("shared/unallocated events must not carry a strategy_book")
        return self


class AccountingDomainEvent(BaseModel):
    """Append-only accounting fact. Identity is (event_type, source, source_id)."""

    event_id: str = Field(min_length=1)
    schema_version: int = EVENT_SCHEMA_VERSION
    event_type: AccountingEventType
    occurred_at: datetime
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    provenance: DataProvenance = DataProvenance.LIVE_PAPER
    payload: AccountingEventPayload = Field(default_factory=AccountingEventPayload)
    sequence: int | None = Field(default=None, ge=1)
    recorded_at: datetime | None = None

    @model_validator(mode="after")
    def validate_identity_and_clock(self) -> AccountingDomainEvent:
        if self.occurred_at.tzinfo is None:
            self.occurred_at = self.occurred_at.replace(tzinfo=UTC)
        if self.recorded_at is not None and self.recorded_at.tzinfo is None:
            self.recorded_at = self.recorded_at.replace(tzinfo=UTC)
        expected = make_event_id(self.event_type, self.source, self.source_id)
        if self.event_id != expected:
            raise ValueError(f"event_id must be {expected}")
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        _require_payload(self.event_type, self.payload)
        return self

    def facts_match(self, other: AccountingDomainEvent) -> bool:
        return (
            self.event_id == other.event_id
            and self.event_type is other.event_type
            and self.source == other.source
            and self.source_id == other.source_id
            and self.provenance is other.provenance
            and self.schema_version == other.schema_version
            and self.payload == other.payload
        )

    def canonical_dump(self) -> dict[str, object]:
        """Projection-stable dump. Sequence/recorded_at are store metadata."""

        return self.model_dump(
            mode="json",
            exclude={"sequence", "recorded_at"},
        )


def make_event_id(
    event_type: AccountingEventType | str,
    source: str,
    source_id: str,
) -> str:
    kind = event_type.value if isinstance(event_type, AccountingEventType) else str(event_type)
    return f"{kind}:{source}:{source_id}"


def _require_payload(event_type: AccountingEventType, payload: AccountingEventPayload) -> None:
    if event_type in {
        AccountingEventType.CAPITAL_INTRODUCED,
        AccountingEventType.CAPITAL_WITHDRAWN,
        AccountingEventType.PAPER_CAPITAL_LOCK,
        AccountingEventType.PAPER_CAPITAL_RELEASE,
        AccountingEventType.SETTLEMENT_PNL,
        AccountingEventType.FEE_POSTED,
        AccountingEventType.MANUAL_ADJUSTMENT,
    }:
        if payload.venue is None or not payload.currency:
            raise ValueError(f"{event_type.value} requires venue and currency")
    if event_type in {
        AccountingEventType.CAPITAL_INTRODUCED,
        AccountingEventType.CAPITAL_WITHDRAWN,
        AccountingEventType.PAPER_CAPITAL_LOCK,
        AccountingEventType.PAPER_CAPITAL_RELEASE,
        AccountingEventType.FEE_POSTED,
    }:
        if payload.amount_native < 0:
            raise ValueError(f"{event_type.value} native amount cannot be negative")
    if event_type is AccountingEventType.TREASURY_TRANSFER:
        if (
            payload.venue is None
            or not payload.currency
            or payload.counterparty_venue is None
            or not payload.counterparty_currency
        ):
            raise ValueError("treasury_transfer requires source and destination pools")
        if payload.amount_native <= 0:
            raise ValueError("treasury_transfer amount must be positive")
    if event_type is AccountingEventType.FX_CONVERSION:
        if (
            payload.venue is None
            or not payload.currency
            or payload.counterparty_venue is None
            or not payload.counterparty_currency
        ):
            raise ValueError("fx_conversion requires both native legs")
        if payload.amount_native <= 0 or not payload.counterparty_amount_native:
            raise ValueError("fx_conversion requires both native amounts")
        if payload.fx_rate_gbp_per_unit is None or not payload.fx_source:
            raise ValueError("fx_conversion requires executed rate provenance")
    if event_type is AccountingEventType.FX_REVALUATION:
        if not payload.currency:
            raise ValueError("fx_revaluation requires currency")
        if payload.fx_rate_gbp_per_unit is None or not payload.fx_source:
            raise ValueError("fx_revaluation requires rate source facts")
    if event_type is AccountingEventType.PAPER_TRADE_OPENED:
        if not payload.trade_id:
            raise ValueError("paper_trade_opened requires trade_id")
    if event_type is AccountingEventType.PAPER_TRADE_FILL:
        if not payload.trade_id or payload.venue is None:
            raise ValueError("paper_trade_fill requires trade_id and venue")
    if event_type is AccountingEventType.MANUAL_ADJUSTMENT:
        if not payload.reason.strip():
            raise ValueError("manual_adjustment requires provenance reason")
        if payload.operator_reference is None and not payload.reason:
            raise ValueError("manual_adjustment requires operator provenance")


def domain_event(
    *,
    event_type: AccountingEventType,
    source: str,
    source_id: str,
    occurred_at: datetime,
    payload: AccountingEventPayload | None = None,
    provenance: DataProvenance = DataProvenance.LIVE_PAPER,
    schema_version: int = EVENT_SCHEMA_VERSION,
) -> AccountingDomainEvent:
    return AccountingDomainEvent(
        event_id=make_event_id(event_type, source, source_id),
        schema_version=schema_version,
        event_type=event_type,
        occurred_at=occurred_at,
        source=source,
        source_id=source_id,
        provenance=provenance,
        payload=payload or AccountingEventPayload(),
    )
