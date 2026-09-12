"""Daily GBP carrying-value revaluation. Native units are never mutated."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sports_hedge.accounting.dimensions import (
    AttributionScope,
    CapitalSource,
    CashState,
    EconomicAccount,
    PostingDimensions,
    PostingSide,
    cash_account,
)
from sports_hedge.accounting.paper_journal import (
    DuplicateJournalError,
    PaperJournal,
    PaperJournalEntry,
    PaperJournalPosting,
)
from sports_hedge.accounting.strategy_books import StrategyBookReporter
from sports_hedge.fx.models import RATE_QUANTUM
from sports_hedge.fx.service import FxRateService

REVALUATION_SOURCE = "daily_fx_revaluation"


class DailyFxRevaluationService:
    def __init__(self, fx: FxRateService, journal: PaperJournal) -> None:
        self.fx = fx
        self.journal = journal
        self.reporter = StrategyBookReporter()

    def run(self, *, valuation_date: date, as_of: datetime) -> list[PaperJournalEntry]:
        for published in self.fx.repository.list_latest_published():
            self.fx.ensure_valuation_rate(published.currency, valuation_date, as_of=as_of)
        posted: list[PaperJournalEntry] = []
        report = self.reporter.report(self.journal.postings())
        for bucket in report.native_pool_balances:
            if bucket.amount_native == 0:
                continue
            currency = bucket.pool.currency
            if currency == "GBP":
                continue
            rate = self.fx.ensure_valuation_rate(currency, valuation_date, as_of=as_of)
            expected_gbp = (bucket.amount_native * rate.gbp_per_unit).quantize(RATE_QUANTUM)
            delta = (expected_gbp - bucket.amount_gbp).quantize(RATE_QUANTUM)
            if delta == 0:
                continue
            source_id = _source_id(
                valuation_date=valuation_date,
                venue=bucket.pool.venue.value,
                currency=currency,
                state=bucket.state.value,
                attribution=bucket.attribution.value,
                capital_source=bucket.capital_source.value,
                strategy_book=bucket.strategy_book.value if bucket.strategy_book else "none",
            )
            entry = _revaluation_entry(
                source_id=source_id,
                valuation_date=valuation_date,
                as_of=as_of,
                venue=bucket.pool.venue,
                currency=currency,
                state=bucket.state,
                delta_gbp=delta,
                rate=rate.gbp_per_unit,
                source_date=rate.source_date,
                status=rate.status.value,
                primary_source=rate.primary_source,
                attribution=bucket.attribution,
                capital_source=bucket.capital_source,
            )
            try:
                posted.append(self.journal.append(entry))
            except DuplicateJournalError:
                continue
        return posted


def _source_id(
    *,
    valuation_date: date,
    venue: str,
    currency: str,
    state: str,
    attribution: str,
    capital_source: str,
    strategy_book: str,
) -> str:
    return (
        f"{valuation_date.isoformat()}:{venue}:{currency}:{state}:"
        f"{attribution}:{capital_source}:{strategy_book}"
    )


def _revaluation_entry(
    *,
    source_id: str,
    valuation_date: date,
    as_of: datetime,
    venue,
    currency: str,
    state: CashState,
    delta_gbp: Decimal,
    rate: Decimal,
    source_date: date,
    status: str,
    primary_source: str,
    attribution: AttributionScope,
    capital_source: CapitalSource,
) -> PaperJournalEntry:
    gain = delta_gbp > 0
    movement = abs(delta_gbp)
    dims = PostingDimensions(
        attribution=AttributionScope.SHARED_UNALLOCATED,
        strategy_book=None,
        capital_source=CapitalSource.SHARED_UNALLOCATED,
        venue=venue,
        currency=currency,
    )
    cash = PaperJournalPosting(
        account_code=cash_account(venue, currency, state),
        side=PostingSide.DEBIT if gain else PostingSide.CREDIT,
        amount_native=Decimal("0"),
        amount_gbp=movement,
        fx_rate_gbp_per_unit=rate,
        dimensions=dims,
    )
    pnl = PaperJournalPosting(
        account_code=EconomicAccount.PNL_FX_UNREALISED.value,
        side=PostingSide.CREDIT if gain else PostingSide.DEBIT,
        amount_native=Decimal("0"),
        amount_gbp=movement,
        fx_rate_gbp_per_unit=rate,
        dimensions=dims,
    )
    return PaperJournalEntry(
        source=REVALUATION_SOURCE,
        source_id=source_id,
        occurred_at=as_of,
        description=(
            f"Daily FX revaluation {valuation_date.isoformat()} {currency} "
            f"{primary_source} source_date={source_date.isoformat()} status={status}"
        ),
        opportunity_id=f"treasury:fx:{valuation_date.isoformat()}",
        postings=[cash, pnl],
    )


__all__ = ["DailyFxRevaluationService", "REVALUATION_SOURCE"]
