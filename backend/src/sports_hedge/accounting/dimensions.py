"""Strategy-book and profit-centre dimensions for a single audited ledger.

`strategy_book` is a journal/posting dimension. It is not encoded by duplicating
economic accounts. Unknown labels are rejected so Research (the product module)
cannot be recorded as if research activity itself were a P&L book.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from sports_hedge.domain.models import VenueName

DIMENSIONS_METADATA_KEY = "strategy_dimensions"


class UnknownStrategyBookError(ValueError):
    """Raised when a strategy label is not a recognised profit-centre book."""


class UnknownCapitalSourceError(ValueError):
    """Raised when a capital-source label is not a recognised funding dimension."""


class StrategyBook(StrEnum):
    """Accounting / strategy books (profit centres).

    RESEARCH_VALUE is directional/value *positions* generated from Research /
    Scenario Response / Value signals. It is not a claim that research activity
    itself is profit.
    """

    ARBITRAGE = "ARBITRAGE"
    RESEARCH_VALUE = "RESEARCH_VALUE"


class ProductModule(StrEnum):
    """User-facing product modules. Distinct from accounting books."""

    ARBITRAGE = "Arbitrage"
    RESEARCH = "Research"


class AttributionScope(StrEnum):
    """Whether a posting can be assigned to a strategy book.

    FX and treasury movements that cannot be attributed reliably stay
    `SHARED_UNALLOCATED` rather than being forced into a strategy.
    """

    STRATEGY = "strategy"
    SHARED_UNALLOCATED = "shared_unallocated"


class CapitalSource(StrEnum):
    """Funding path for capital used on a posting.

    Distinct from `strategy_book` and from native venue/currency pools. Ordinary
    automated liquidity is `AUTO_POOL`; one-off Priority Arb escalations of
    Sports Hedge capital are `MANUAL_OVERRIDE`. `MANUAL_EXTERNAL` is an
    externally confirmed venue leg (for example a manual USD fill) and does
    not imply Sports Hedge custody or an automated-pool draw.
    `SHARED_UNALLOCATED` remains treasury attribution when funding cannot be
    assigned to those sources. `PAPER_SIMULATED_EXTERNAL` is a paper-only
    simulated fill of a leg that would later require EXTERNAL_OPERATOR handling.
    It is not operator MANUAL_EXTERNAL confirmation and not live custody.
    This is a posting dimension, not a duplicated cash account.
    """

    AUTO_POOL = "AUTO_POOL"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    MANUAL_EXTERNAL = "MANUAL_EXTERNAL"
    PAPER_SIMULATED_EXTERNAL = "PAPER_SIMULATED_EXTERNAL"
    SHARED_UNALLOCATED = "SHARED_UNALLOCATED"

    def implies_sports_hedge_custody(self) -> bool:
        return self in {CapitalSource.AUTO_POOL, CapitalSource.MANUAL_OVERRIDE}

    def draws_automated_pool(self) -> bool:
        return self is CapitalSource.AUTO_POOL


_CAPITAL_SOURCE_ALIASES = {
    "AUTO": CapitalSource.AUTO_POOL,
    "AUTO_POOL": CapitalSource.AUTO_POOL,
    "MANUAL_OVERRIDE": CapitalSource.MANUAL_OVERRIDE,
    "MANUAL_EXTERNAL": CapitalSource.MANUAL_EXTERNAL,
    "EXTERNAL": CapitalSource.MANUAL_EXTERNAL,
    "PAPER_SIMULATED_EXTERNAL": CapitalSource.PAPER_SIMULATED_EXTERNAL,
    "PAPER_SIMULATED": CapitalSource.PAPER_SIMULATED_EXTERNAL,
    "SHARED": CapitalSource.SHARED_UNALLOCATED,
    "UNALLOCATED": CapitalSource.SHARED_UNALLOCATED,
    "SHARED_UNALLOCATED": CapitalSource.SHARED_UNALLOCATED,
    "SHARED/UNALLOCATED": CapitalSource.SHARED_UNALLOCATED,
}


class PostingSide(StrEnum):
    """Matches the FX ledger posting sides so these facts can wrap journals later."""

    DEBIT = "debit"
    CREDIT = "credit"


class CashState(StrEnum):
    AVAILABLE = "AVAILABLE"
    LOCKED = "LOCKED"
    TRANSIT = "TRANSIT"


class EconomicAccount(StrEnum):
    """Chart-of-accounts families. Strategy is not part of the account code."""

    PNL_BETTING = "PNL:BETTING"
    PNL_VENUE_FEES = "PNL:VENUE_FEES"
    PNL_FX_REALISED = "PNL:FX:REALISED"
    PNL_FX_UNREALISED = "PNL:FX:UNREALISED"


_PRODUCT_MODULE_BY_BOOK = {
    StrategyBook.ARBITRAGE: ProductModule.ARBITRAGE,
    StrategyBook.RESEARCH_VALUE: ProductModule.RESEARCH,
}


def parse_strategy_book(label: str | StrategyBook) -> StrategyBook:
    if isinstance(label, StrategyBook):
        return label
    if not isinstance(label, str) or not label.strip():
        raise UnknownStrategyBookError("strategy book label is required")
    normalised = label.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return StrategyBook(normalised)
    except ValueError as exc:
        raise UnknownStrategyBookError(f"unknown strategy book: {label!r}") from exc


def product_module_for(book: StrategyBook | str) -> ProductModule:
    return _PRODUCT_MODULE_BY_BOOK[parse_strategy_book(book)]


def parse_capital_source(label: str | CapitalSource) -> CapitalSource:
    if isinstance(label, CapitalSource):
        return label
    if not isinstance(label, str) or not label.strip():
        raise UnknownCapitalSourceError("capital source label is required")
    normalised = label.strip().upper().replace("-", "_").replace(" ", "_")
    if normalised in _CAPITAL_SOURCE_ALIASES:
        return _CAPITAL_SOURCE_ALIASES[normalised]
    slash_form = label.strip().upper().replace(" ", "")
    if slash_form in _CAPITAL_SOURCE_ALIASES:
        return _CAPITAL_SOURCE_ALIASES[slash_form]
    raise UnknownCapitalSourceError(f"unknown capital source: {label!r}")


def cash_account(venue: VenueName, currency: str, state: CashState | str) -> str:
    bucket = CashState(state) if not isinstance(state, CashState) else state
    return f"ASSET:CASH:{bucket.value}:{venue.value}:{currency.upper()}"


class NativeLiquidityPool(BaseModel):
    """Native venue/currency pool. Never add USD and GBP natives together."""

    venue: VenueName
    currency: str = Field(min_length=3, max_length=12)

    @field_validator("currency", mode="before")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return str(value).upper()

    @property
    def pool_id(self) -> str:
        return f"{self.venue.value}/{self.currency}"

    @property
    def display_name(self) -> str:
        return f"{self.venue.value.title()} / {self.currency}"


WELL_KNOWN_NATIVE_POOLS: tuple[NativeLiquidityPool, ...] = (
    NativeLiquidityPool(venue=VenueName.POLYMARKET, currency="USD"),
    NativeLiquidityPool(venue=VenueName.MATCHBOOK, currency="GBP"),
    NativeLiquidityPool(venue=VenueName.SMARKETS, currency="GBP"),
)


class PostingDimensions(BaseModel):
    """Profit-centre tags attached to a journal posting.

    Optional fields remain optional so treasury/FX journals can omit event
    identity. `strategy_book` is required only when attribution is STRATEGY.
    `capital_source` is independent of both the book and the native pool.
    """

    attribution: AttributionScope = AttributionScope.STRATEGY
    strategy_book: StrategyBook | None = None
    capital_source: CapitalSource = CapitalSource.AUTO_POOL
    venue: VenueName | None = None
    currency: str = Field(min_length=3, max_length=12)
    competition: str | None = None
    canonical_event_id: str | None = None
    position_id: str | None = None
    opportunity_id: str | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return str(value).upper()

    @field_validator("strategy_book", mode="before")
    @classmethod
    def validate_strategy_book(cls, value: StrategyBook | str | None) -> StrategyBook | None:
        if value is None or value == "":
            return None
        return parse_strategy_book(value)

    @field_validator("capital_source", mode="before")
    @classmethod
    def validate_capital_source(cls, value: CapitalSource | str | None) -> CapitalSource:
        if value is None or value == "":
            return CapitalSource.AUTO_POOL
        return parse_capital_source(value)

    @model_validator(mode="after")
    def attribution_matches_book(self) -> PostingDimensions:
        if self.attribution == AttributionScope.STRATEGY:
            if self.strategy_book is None:
                raise ValueError("STRATEGY attribution requires a strategy_book")
        elif self.strategy_book is not None:
            raise ValueError(
                "shared/unallocated treasury postings must not carry a strategy_book"
            )
        return self

    @property
    def native_pool(self) -> NativeLiquidityPool | None:
        if self.venue is None:
            return None
        return NativeLiquidityPool(venue=self.venue, currency=self.currency)

    def to_metadata(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        return {DIMENSIONS_METADATA_KEY: payload}

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any] | None) -> PostingDimensions | None:
        if not metadata:
            return None
        payload = metadata.get(DIMENSIONS_METADATA_KEY, metadata)
        if not isinstance(payload, Mapping):
            return None
        if "currency" not in payload and "attribution" not in payload:
            return None
        return cls.model_validate(payload)
