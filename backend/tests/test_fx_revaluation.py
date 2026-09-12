from __future__ import annotations

from datetime import UTC, date, datetime
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
from sports_hedge.accounting.paper_journal import PaperJournal, PaperJournalEntry, PaperJournalPosting
from sports_hedge.accounting.revaluation import DailyFxRevaluationService, REVALUATION_SOURCE
from sports_hedge.accounting.strategy_books import StrategyBookReporter
from sports_hedge.domain.models import VenueName
from sports_hedge.fx.models import PublishedFxClose
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService


OPENING = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _fx() -> FxRateService:
    service = FxRateService(SqliteFxRateRepository())
    service.persist_ecb_closes(
        [
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=Decimal("0.75000000"),
                source_date=date(2026, 9, 11),
                retrieved_at=OPENING,
                source="ecb_eurofxref",
                source_id="ecb:2026-09-11:USD",
            )
        ]
    )
    return service


def _opening_usd(journal: PaperJournal, *, native: Decimal, gbp: Decimal, rate: Decimal) -> None:
    journal.append(
        PaperJournalEntry(
            source="opening",
            source_id="opening:polymarket:USD:AVAILABLE",
            occurred_at=OPENING,
            description="Opening native USD treasury",
            opportunity_id="opening",
            postings=[
                PaperJournalPosting(
                    account_code=cash_account(VenueName.POLYMARKET, "USD", CashState.AVAILABLE),
                    side=PostingSide.DEBIT,
                    amount_native=native,
                    amount_gbp=gbp,
                    fx_rate_gbp_per_unit=rate,
                    dimensions=PostingDimensions(
                        attribution=AttributionScope.SHARED_UNALLOCATED,
                        capital_source=CapitalSource.SHARED_UNALLOCATED,
                        venue=VenueName.POLYMARKET,
                        currency="USD",
                    ),
                ),
                PaperJournalPosting(
                    account_code="EQUITY:OPENING",
                    side=PostingSide.CREDIT,
                    amount_native=gbp,
                    amount_gbp=gbp,
                    fx_rate_gbp_per_unit=Decimal("1"),
                    dimensions=PostingDimensions(
                        attribution=AttributionScope.SHARED_UNALLOCATED,
                        capital_source=CapitalSource.SHARED_UNALLOCATED,
                        venue=VenueName.POLYMARKET,
                        currency="GBP",
                    ),
                ),
            ],
        )
    )


def test_revaluation_posts_gbp_movement_only_and_is_idempotent() -> None:
    fx = _fx()
    fx.persist_ecb_closes(
        [
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=Decimal("0.80000000"),
                source_date=date(2026, 9, 12),
                retrieved_at=datetime(2026, 9, 12, 15, 0, tzinfo=UTC),
                source="ecb_eurofxref",
                source_id="ecb:2026-09-12:USD",
            )
        ]
    )
    journal = PaperJournal()
    native = Decimal("1000")
    _opening_usd(journal, native=native, gbp=Decimal("750"), rate=Decimal("0.75"))
    service = DailyFxRevaluationService(fx, journal)
    as_of = datetime(2026, 9, 12, 15, 5, tzinfo=UTC)
    first = service.run(valuation_date=date(2026, 9, 12), as_of=as_of)
    assert len(first) == 1
    entry = first[0]
    assert entry.source == REVALUATION_SOURCE
    natives = [posting.amount_native for posting in entry.postings]
    assert natives == [Decimal("0"), Decimal("0")]
    gbp_movement = {posting.side: posting.amount_gbp for posting in entry.postings}
    assert gbp_movement[PostingSide.DEBIT] == Decimal("50.00000000")
    assert gbp_movement[PostingSide.CREDIT] == Decimal("50.00000000")
    assert any(posting.account_code == EconomicAccount.PNL_FX_UNREALISED.value for posting in entry.postings)

    second = service.run(valuation_date=date(2026, 9, 12), as_of=as_of)
    assert second == []
    report = StrategyBookReporter().report(journal.postings())
    usd = next(
        row
        for row in report.native_pool_balances
        if row.pool.venue is VenueName.POLYMARKET and row.pool.currency == "USD"
    )
    assert usd.amount_native == native
    assert usd.amount_gbp == Decimal("800.00000000")
    assert report.shared_treasury.allocated_unrealised_fx_gbp == Decimal("50.00000000")
