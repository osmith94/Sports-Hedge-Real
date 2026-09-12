"""Smallest append-only paper journal / reconciliation seam.

Not a full general ledger. Posts balanced GBP facts with native amounts so a
paper fill can be traced without mixing GBP and USD natives.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.dimensions import (
    AttributionScope,
    CapitalSource,
    CashState,
    PostingDimensions,
    PostingSide,
    StrategyBook,
    cash_account,
)
from sports_hedge.accounting.strategy_books import DimensionedPosting
from sports_hedge.domain.models import VenueName


class UnbalancedJournalError(ValueError):
    """Raised when GBP debits do not equal GBP credits."""


class DuplicateJournalError(ValueError):
    """Raised when (source, source_id) is reused."""


class NativeCurrencyMixError(ValueError):
    """Raised if a caller sums native amounts across currencies."""


class DataProvenance(StrEnum):
    LIVE_PAPER = "live_paper"
    FIXTURE_DEMO = "fixture_demo"
    UNAVAILABLE = "unavailable"


class PaperJournalPosting(BaseModel):
    account_code: str = Field(min_length=1)
    side: PostingSide
    amount_native: Decimal = Field(ge=0)
    amount_gbp: Decimal = Field(ge=0)
    fx_rate_gbp_per_unit: Decimal | None = Field(default=None, gt=0)
    dimensions: PostingDimensions

    def as_dimensioned(self, *, journal_id: str, posting_id: str) -> DimensionedPosting:
        return DimensionedPosting(
            account_code=self.account_code,
            side=self.side,
            amount_native=self.amount_native,
            amount_gbp=self.amount_gbp,
            fx_rate_gbp_per_unit=self.fx_rate_gbp_per_unit,
            dimensions=self.dimensions,
            journal_id=journal_id,
            posting_id=posting_id,
        )


class PaperJournalEntry(BaseModel):
    journal_id: str = Field(default_factory=lambda: str(uuid4()))
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    occurred_at: datetime
    description: str
    opportunity_id: str
    provenance: DataProvenance = DataProvenance.LIVE_PAPER
    postings: list[PaperJournalPosting] = Field(min_length=2)

    @model_validator(mode="after")
    def must_balance_in_gbp(self) -> PaperJournalEntry:
        if self.occurred_at.tzinfo is None:
            self.occurred_at = self.occurred_at.replace(tzinfo=UTC)
        debits = sum(
            (item.amount_gbp for item in self.postings if item.side is PostingSide.DEBIT),
            Decimal("0"),
        )
        credits = sum(
            (item.amount_gbp for item in self.postings if item.side is PostingSide.CREDIT),
            Decimal("0"),
        )
        if debits != credits:
            raise UnbalancedJournalError(
                f"journal {self.source_id} is unbalanced in GBP: debit={debits} credit={credits}"
            )
        return self


class PaperJournal:
    """In-memory append-only journal. Corrections would require a reversing entry."""

    def __init__(self) -> None:
        self._entries: list[PaperJournalEntry] = []
        self._ids: set[tuple[str, str]] = set()

    def append(self, entry: PaperJournalEntry) -> PaperJournalEntry:
        key = (entry.source, entry.source_id)
        if key in self._ids:
            raise DuplicateJournalError(f"duplicate journal {entry.source}:{entry.source_id}")
        self._ids.add(key)
        self._entries.append(entry)
        return entry

    def list_entries(self, *, opportunity_id: str | None = None) -> list[PaperJournalEntry]:
        if opportunity_id is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.opportunity_id == opportunity_id]

    def postings(self, *, opportunity_id: str | None = None) -> list[DimensionedPosting]:
        facts: list[DimensionedPosting] = []
        for entry in self.list_entries(opportunity_id=opportunity_id):
            for index, posting in enumerate(entry.postings):
                facts.append(
                    posting.as_dimensioned(
                        journal_id=entry.journal_id,
                        posting_id=f"{entry.journal_id}:{index}",
                    )
                )
        return facts


def cash_lock_postings(
    *,
    venue: VenueName,
    currency: str,
    amount_native: Decimal,
    amount_gbp: Decimal,
    fx_rate_gbp_per_unit: Decimal,
    opportunity_id: str,
    capital_source: CapitalSource,
    canonical_event_id: str | None = None,
    position_id: str | None = None,
) -> list[PaperJournalPosting]:
    """Move available native cash to locked. Balanced in GBP. Does not invent P&L."""

    dims = PostingDimensions(
        attribution=AttributionScope.STRATEGY,
        strategy_book=StrategyBook.ARBITRAGE,
        capital_source=capital_source,
        venue=venue,
        currency=currency,
        opportunity_id=opportunity_id,
        canonical_event_id=canonical_event_id,
        position_id=position_id,
    )
    return [
        PaperJournalPosting(
            account_code=cash_account(venue, currency, CashState.LOCKED),
            side=PostingSide.DEBIT,
            amount_native=amount_native,
            amount_gbp=amount_gbp,
            fx_rate_gbp_per_unit=fx_rate_gbp_per_unit,
            dimensions=dims,
        ),
        PaperJournalPosting(
            account_code=cash_account(venue, currency, CashState.AVAILABLE),
            side=PostingSide.CREDIT,
            amount_native=amount_native,
            amount_gbp=amount_gbp,
            fx_rate_gbp_per_unit=fx_rate_gbp_per_unit,
            dimensions=dims,
        ),
    ]


def assert_native_currencies_separate(postings: Iterable[DimensionedPosting]) -> None:
    currencies = {item.dimensions.currency for item in postings}
    if len(currencies) > 1:
        natives = {item.dimensions.currency: item.amount_native for item in postings}
        if len({item.dimensions.currency for item in postings}) > 1:
            raise NativeCurrencyMixError(
                f"native amounts remain separate: {sorted(natives)}"
            )


def gbp_is_balanced(postings: Iterable[DimensionedPosting]) -> bool:
    signed = sum((item.signed_gbp for item in postings), Decimal("0"))
    return signed == Decimal("0")
