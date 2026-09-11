"""GBP profit-centre reporting over dimensioned ledger facts.

This is a query helper, not a second ledger. Totals are derived from posting
facts that already carry native amounts and GBP presentation values from the
accounting FX layer. Native USD and GBP balances are never added together.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.dimensions import (
    WELL_KNOWN_NATIVE_POOLS,
    AttributionScope,
    CapitalSource,
    CashState,
    EconomicAccount,
    NativeLiquidityPool,
    PostingDimensions,
    PostingSide,
    StrategyBook,
    cash_account,
    parse_capital_source,
    parse_strategy_book,
)
from sports_hedge.domain.models import VenueName

_ZERO = Decimal(0)
_CASH_PREFIX = "ASSET:CASH:"
_CASH_STATES = {state.value: state for state in CashState}
_PNL_ACCOUNTS = {account.value: account for account in EconomicAccount}


class NativeCurrencyMixError(ValueError):
    """Raised if a caller attempts to combine native amounts across currencies."""


class MissingGbpPresentationError(ValueError):
    """Raised when a GBP total is requested without FX-layer presentation amounts."""


def missing_gbp_presentation_error(currency: str) -> MissingGbpPresentationError:
    return MissingGbpPresentationError(
        f"GBP presentation for {currency} must come from the accounting FX layer; "
        "native balances cannot be treated as GBP"
    )


class DimensionedPosting(BaseModel):
    """Reporting fact derived from an append-only journal posting.

    `amount_gbp` must already be the FX-layer functional-currency amount. This
    object does not convert native balances at 1:1.
    """

    account_code: str = Field(min_length=1)
    side: PostingSide
    amount_native: Decimal = Field(ge=0)
    amount_gbp: Decimal = Field(ge=0)
    fx_rate_gbp_per_unit: Decimal | None = Field(default=None, gt=0)
    dimensions: PostingDimensions
    journal_id: str | None = None
    posting_id: str | None = None

    @model_validator(mode="after")
    def currency_and_gbp_are_consistent(self) -> DimensionedPosting:
        if self.dimensions.currency == "GBP":
            if self.fx_rate_gbp_per_unit is None:
                self.fx_rate_gbp_per_unit = Decimal(1)
            if self.amount_gbp != self.amount_native:
                raise ValueError("GBP postings must have amount_gbp equal to amount_native")
            if self.fx_rate_gbp_per_unit != Decimal(1):
                raise ValueError("GBP functional-currency rate must equal 1")
        elif self.fx_rate_gbp_per_unit is None:
            raise missing_gbp_presentation_error(self.dimensions.currency)
        return self

    @property
    def signed_native(self) -> Decimal:
        return self.amount_native if self.side == PostingSide.DEBIT else -self.amount_native

    @property
    def signed_gbp(self) -> Decimal:
        return self.amount_gbp if self.side == PostingSide.DEBIT else -self.amount_gbp


class CapitalBucketTotals(BaseModel):
    available_gbp: Decimal = _ZERO
    locked_gbp: Decimal = _ZERO
    transit_gbp: Decimal = _ZERO

    @property
    def total_gbp(self) -> Decimal:
        return self.available_gbp + self.locked_gbp + self.transit_gbp


class NativePoolBalance(BaseModel):
    pool: NativeLiquidityPool
    state: CashState
    amount_native: Decimal
    amount_gbp: Decimal
    strategy_book: StrategyBook | None = None
    attribution: AttributionScope
    capital_source: CapitalSource = CapitalSource.AUTO_POOL


class ProfitCentreTotals(BaseModel):
    strategy_book: StrategyBook | None = None
    attribution: AttributionScope
    realised_betting_pnl_gbp: Decimal = _ZERO
    venue_fees_gbp: Decimal = _ZERO
    allocated_realised_fx_gbp: Decimal = _ZERO
    allocated_unrealised_fx_gbp: Decimal = _ZERO
    capital: CapitalBucketTotals = Field(default_factory=CapitalBucketTotals)
    capital_by_source: dict[CapitalSource, CapitalBucketTotals] = Field(
        default_factory=lambda: {source: CapitalBucketTotals() for source in CapitalSource}
    )

    @property
    def net_pnl_gbp(self) -> Decimal:
        return (
            self.realised_betting_pnl_gbp
            - self.venue_fees_gbp
            + self.allocated_realised_fx_gbp
            + self.allocated_unrealised_fx_gbp
        )


class StrategyBookReport(BaseModel):
    by_strategy: dict[StrategyBook, ProfitCentreTotals]
    shared_treasury: ProfitCentreTotals
    native_pool_balances: list[NativePoolBalance]
    capital_by_source: dict[CapitalSource, CapitalBucketTotals]
    well_known_pools: tuple[NativeLiquidityPool, ...] = WELL_KNOWN_NATIVE_POOLS

    def totals_for(self, book: StrategyBook | str) -> ProfitCentreTotals:
        return self.by_strategy[parse_strategy_book(book)]

    def capital_for_source(self, source: CapitalSource | str) -> CapitalBucketTotals:
        return self.capital_by_source[parse_capital_source(source)]

    def native_balances_for(
        self,
        *,
        venue: VenueName,
        currency: str,
        strategy_book: StrategyBook | None = None,
        capital_source: CapitalSource | str | None = None,
        include_shared: bool = True,
    ) -> list[NativePoolBalance]:
        currency = currency.upper()
        rows = [
            row
            for row in self.native_pool_balances
            if row.pool.venue == venue and row.pool.currency == currency
        ]
        if capital_source is not None:
            source = parse_capital_source(capital_source)
            rows = [row for row in rows if row.capital_source == source]
        if strategy_book is None and include_shared:
            return rows
        return [
            row
            for row in rows
            if row.strategy_book == strategy_book
            or (include_shared and row.attribution == AttributionScope.SHARED_UNALLOCATED)
        ]


def parse_cash_account(account_code: str) -> tuple[CashState, VenueName, str] | None:
    if not account_code.startswith(_CASH_PREFIX):
        return None
    parts = account_code.split(":")
    if len(parts) != 5:
        return None
    _, _, state_raw, venue_raw, currency = parts
    state = _CASH_STATES.get(state_raw.upper())
    if state is None:
        return None
    try:
        venue = VenueName(venue_raw.lower())
    except ValueError:
        return None
    return state, venue, currency.upper()


def classify_pnl_account(account_code: str) -> EconomicAccount | None:
    return _PNL_ACCOUNTS.get(account_code)


def _empty_strategy_totals() -> dict[StrategyBook, ProfitCentreTotals]:
    return {
        book: ProfitCentreTotals(strategy_book=book, attribution=AttributionScope.STRATEGY)
        for book in StrategyBook
    }


def _centre_for(
    posting: DimensionedPosting,
    by_strategy: dict[StrategyBook, ProfitCentreTotals],
    shared: ProfitCentreTotals,
) -> ProfitCentreTotals:
    dims = posting.dimensions
    if dims.attribution == AttributionScope.SHARED_UNALLOCATED or dims.strategy_book is None:
        return shared
    return by_strategy[dims.strategy_book]


def _apply_pnl(totals: ProfitCentreTotals, posting: DimensionedPosting) -> None:
    account = classify_pnl_account(posting.account_code)
    if account is None:
        return
    # Credits to P&L income accounts are profit; debits to fee/expense accounts
    # increase costs. signed_gbp is debit-positive.
    if account == EconomicAccount.PNL_BETTING:
        totals.realised_betting_pnl_gbp -= posting.signed_gbp
    elif account == EconomicAccount.PNL_VENUE_FEES:
        totals.venue_fees_gbp += posting.signed_gbp
    elif account == EconomicAccount.PNL_FX_REALISED:
        totals.allocated_realised_fx_gbp -= posting.signed_gbp
    elif account == EconomicAccount.PNL_FX_UNREALISED:
        totals.allocated_unrealised_fx_gbp -= posting.signed_gbp


def _apply_capital(
    totals: ProfitCentreTotals,
    posting: DimensionedPosting,
    state: CashState,
) -> None:
    signed = posting.signed_gbp
    _add_bucket(totals.capital, state, signed)
    source = posting.dimensions.capital_source
    _add_bucket(totals.capital_by_source[source], state, signed)


def _add_bucket(totals: CapitalBucketTotals, state: CashState, signed: Decimal) -> None:
    if state == CashState.AVAILABLE:
        totals.available_gbp += signed
    elif state == CashState.LOCKED:
        totals.locked_gbp += signed
    else:
        totals.transit_gbp += signed


def assert_single_native_currency(postings: Iterable[DimensionedPosting]) -> str:
    currencies = {posting.dimensions.currency for posting in postings}
    if len(currencies) != 1:
        raise NativeCurrencyMixError(
            f"cannot combine native amounts across currencies {sorted(currencies)}"
        )
    return next(iter(currencies))


def sum_native(postings: Iterable[DimensionedPosting]) -> Decimal:
    items = list(postings)
    if not items:
        return _ZERO
    assert_single_native_currency(items)
    return sum((posting.signed_native for posting in items), _ZERO)


class StrategyBookReporter:
    """Aggregate GBP P&L and capital by strategy without mixing native currencies."""

    def report(self, postings: Iterable[DimensionedPosting]) -> StrategyBookReport:
        by_strategy = _empty_strategy_totals()
        shared = ProfitCentreTotals(
            strategy_book=None,
            attribution=AttributionScope.SHARED_UNALLOCATED,
        )
        capital_by_source = {source: CapitalBucketTotals() for source in CapitalSource}
        native_buckets: dict[
            tuple[str, str, CashState, StrategyBook | None, AttributionScope, CapitalSource],
            list[DimensionedPosting],
        ] = defaultdict(list)

        for posting in postings:
            totals = _centre_for(posting, by_strategy, shared)
            _apply_pnl(totals, posting)
            parsed_cash = parse_cash_account(posting.account_code)
            if parsed_cash is not None:
                state, venue, currency = parsed_cash
                if posting.dimensions.venue is not None and posting.dimensions.venue != venue:
                    raise ValueError("posting venue dimension does not match cash account")
                if posting.dimensions.currency != currency:
                    raise ValueError("posting currency dimension does not match cash account")
                _apply_capital(totals, posting, state)
                _add_bucket(
                    capital_by_source[posting.dimensions.capital_source],
                    state,
                    posting.signed_gbp,
                )
                dims = posting.dimensions
                native_buckets[
                    (
                        venue.value,
                        currency,
                        state,
                        dims.strategy_book,
                        dims.attribution,
                        dims.capital_source,
                    )
                ].append(posting)

        native_pool_balances: list[NativePoolBalance] = []
        for (venue_raw, currency, state, book, attribution, source), group in sorted(
            native_buckets.items(),
            key=lambda item: (
                item[0][0],
                item[0][1],
                item[0][2].value,
                str(item[0][3]),
                item[0][4].value,
                item[0][5].value,
            ),
        ):
            # Native sum is safe: grouped by currency. GBP is summed separately.
            native_pool_balances.append(
                NativePoolBalance(
                    pool=NativeLiquidityPool(venue=VenueName(venue_raw), currency=currency),
                    state=state,
                    amount_native=sum_native(group),
                    amount_gbp=sum((item.signed_gbp for item in group), _ZERO),
                    strategy_book=book,
                    attribution=attribution,
                    capital_source=source,
                )
            )

        return StrategyBookReport(
            by_strategy=by_strategy,
            shared_treasury=shared,
            native_pool_balances=native_pool_balances,
            capital_by_source=capital_by_source,
        )


def strategy_cash_account(
    book: StrategyBook | str,
    venue: VenueName,
    currency: str,
    state: CashState | str,
) -> str:
    """Economic cash account. Strategy remains a posting dimension, not the code."""
    parse_strategy_book(book)
    return cash_account(venue, currency, state)


def posting_from_mapping(payload: Mapping[str, object]) -> DimensionedPosting:
    return DimensionedPosting.model_validate(payload)
