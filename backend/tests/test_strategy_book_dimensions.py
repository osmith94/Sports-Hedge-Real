from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from sports_hedge.accounting.dimensions import (
    DIMENSIONS_METADATA_KEY,
    WELL_KNOWN_NATIVE_POOLS,
    AttributionScope,
    CapitalSource,
    CashState,
    EconomicAccount,
    PostingDimensions,
    PostingSide,
    ProductModule,
    StrategyBook,
    UnknownCapitalSourceError,
    UnknownStrategyBookError,
    cash_account,
    parse_capital_source,
    parse_strategy_book,
    product_module_for,
)
from sports_hedge.accounting.strategy_books import (
    DimensionedPosting,
    NativeCurrencyMixError,
    StrategyBookReporter,
    missing_gbp_presentation_error,
    strategy_cash_account,
    sum_native,
)
from sports_hedge.domain.models import VenueName

USD_RATE = Decimal("0.74")


def _dims(
    book: StrategyBook | None,
    *,
    venue: VenueName,
    currency: str,
    attribution: AttributionScope | None = None,
    capital_source: CapitalSource | str | None = None,
    **kwargs: object,
) -> PostingDimensions:
    if attribution is None:
        attribution = (
            AttributionScope.STRATEGY if book is not None else AttributionScope.SHARED_UNALLOCATED
        )
    payload: dict[str, object] = {
        "attribution": attribution,
        "strategy_book": book,
        "venue": venue,
        "currency": currency,
        **kwargs,
    }
    if capital_source is not None:
        payload["capital_source"] = capital_source
    return PostingDimensions.model_validate(payload)


def _posting(
    account: str,
    side: PostingSide,
    native: str | Decimal,
    dims: PostingDimensions,
    *,
    rate: Decimal | None = None,
) -> DimensionedPosting:
    amount_native = Decimal(native)
    if dims.currency == "GBP":
        gbp = amount_native
        rate = Decimal(1)
    else:
        if rate is None:
            rate = USD_RATE
        gbp = amount_native * rate
    return DimensionedPosting(
        account_code=account,
        side=side,
        amount_native=amount_native,
        amount_gbp=gbp,
        fx_rate_gbp_per_unit=rate,
        dimensions=dims,
    )


def test_strategy_book_enum_is_typed_and_rejects_unknown_labels() -> None:
    assert parse_strategy_book("ARBITRAGE") is StrategyBook.ARBITRAGE
    assert parse_strategy_book("research_value") is StrategyBook.RESEARCH_VALUE
    with pytest.raises(UnknownStrategyBookError, match="unknown strategy book"):
        parse_strategy_book("RESEARCH")
    with pytest.raises(UnknownStrategyBookError):
        parse_strategy_book("DIRECTIONAL")


def test_product_modules_are_distinct_from_accounting_books() -> None:
    assert product_module_for(StrategyBook.ARBITRAGE) is ProductModule.ARBITRAGE
    assert product_module_for(StrategyBook.RESEARCH_VALUE) is ProductModule.RESEARCH
    assert ProductModule.RESEARCH.value == "Research"
    assert StrategyBook.RESEARCH_VALUE.value == "RESEARCH_VALUE"


def test_strategy_attribution_requires_a_book_and_shared_treasury_forbids_one() -> None:
    with pytest.raises(ValidationError, match="STRATEGY attribution requires"):
        PostingDimensions(
            attribution=AttributionScope.STRATEGY,
            venue=VenueName.MATCHBOOK,
            currency="GBP",
        )
    with pytest.raises(ValidationError, match="must not carry a strategy_book"):
        PostingDimensions(
            attribution=AttributionScope.SHARED_UNALLOCATED,
            strategy_book=StrategyBook.ARBITRAGE,
            venue=VenueName.MATCHBOOK,
            currency="GBP",
        )


def test_strategy_is_a_posting_dimension_not_a_duplicated_account() -> None:
    arb = strategy_cash_account(StrategyBook.ARBITRAGE, VenueName.MATCHBOOK, "GBP", CashState.AVAILABLE)
    value = strategy_cash_account("RESEARCH_VALUE", VenueName.MATCHBOOK, "GBP", CashState.AVAILABLE)
    assert arb == value == "ASSET:CASH:AVAILABLE:matchbook:GBP"
    assert "ARBITRAGE" not in arb
    assert "AUTO_POOL" not in arb
    assert "MANUAL_OVERRIDE" not in arb


def test_capital_source_is_typed_and_rejects_unknown_labels() -> None:
    assert parse_capital_source("AUTO_POOL") is CapitalSource.AUTO_POOL
    assert parse_capital_source("manual-override") is CapitalSource.MANUAL_OVERRIDE
    assert parse_capital_source("SHARED/UNALLOCATED") is CapitalSource.SHARED_UNALLOCATED
    with pytest.raises(UnknownCapitalSourceError, match="unknown capital source"):
        parse_capital_source("PRIORITY_ARB")
    with pytest.raises(ValidationError, match="unknown capital source"):
        PostingDimensions(
            attribution=AttributionScope.STRATEGY,
            strategy_book=StrategyBook.ARBITRAGE,
            capital_source="HOUSE_BANK",
            venue=VenueName.MATCHBOOK,
            currency="GBP",
        )


def test_capital_source_is_independent_of_strategy_book_and_native_pool() -> None:
    dims = _dims(
        StrategyBook.ARBITRAGE,
        venue=VenueName.MATCHBOOK,
        currency="GBP",
        capital_source=CapitalSource.MANUAL_OVERRIDE,
    )
    assert dims.strategy_book is StrategyBook.ARBITRAGE
    assert dims.capital_source is CapitalSource.MANUAL_OVERRIDE
    assert dims.native_pool is not None
    assert dims.native_pool.pool_id == "matchbook/GBP"
    shared_manual = _dims(
        None,
        venue=VenueName.MATCHBOOK,
        currency="GBP",
        capital_source="MANUAL_OVERRIDE",
    )
    assert shared_manual.strategy_book is None
    assert shared_manual.capital_source is CapitalSource.MANUAL_OVERRIDE


def test_dimensions_round_trip_through_journal_metadata() -> None:
    dims = _dims(
        StrategyBook.RESEARCH_VALUE,
        venue=VenueName.POLYMARKET,
        currency="usd",
        competition="premier league",
        canonical_event_id="evt:1",
        position_id="pos:1",
        opportunity_id="opp:1",
        capital_source=CapitalSource.MANUAL_OVERRIDE,
    )
    metadata = {"source": "paper", **dims.to_metadata()}
    restored = PostingDimensions.from_metadata(metadata)
    assert restored == dims
    assert DIMENSIONS_METADATA_KEY in metadata


def test_arbitrage_and_research_value_pnl_are_segregated() -> None:
    matchbook_gbp = _dims(StrategyBook.ARBITRAGE, venue=VenueName.MATCHBOOK, currency="GBP")
    polymarket_usd = _dims(StrategyBook.RESEARCH_VALUE, venue=VenueName.POLYMARKET, currency="USD")
    postings = [
        _posting(EconomicAccount.PNL_BETTING, PostingSide.CREDIT, "40", matchbook_gbp),
        _posting(EconomicAccount.PNL_VENUE_FEES, PostingSide.DEBIT, "4", matchbook_gbp),
        _posting(EconomicAccount.PNL_BETTING, PostingSide.CREDIT, "20", polymarket_usd),
        _posting(EconomicAccount.PNL_VENUE_FEES, PostingSide.DEBIT, "1", polymarket_usd),
    ]

    report = StrategyBookReporter().report(postings)
    arb = report.totals_for(StrategyBook.ARBITRAGE)
    value = report.totals_for(StrategyBook.RESEARCH_VALUE)

    assert arb.realised_betting_pnl_gbp == Decimal(40)
    assert arb.venue_fees_gbp == Decimal(4)
    assert arb.net_pnl_gbp == Decimal(36)
    assert value.realised_betting_pnl_gbp == Decimal(20) * USD_RATE
    assert value.venue_fees_gbp == Decimal(1) * USD_RATE
    assert value.net_pnl_gbp == Decimal(19) * USD_RATE
    assert report.shared_treasury.net_pnl_gbp == Decimal(0)


def test_unattributable_fx_stays_in_shared_treasury() -> None:
    attributed = _dims(StrategyBook.ARBITRAGE, venue=VenueName.POLYMARKET, currency="USD")
    shared = _dims(None, venue=VenueName.POLYMARKET, currency="USD")
    postings = [
        _posting(EconomicAccount.PNL_FX_REALISED, PostingSide.CREDIT, "10", attributed),
        _posting(EconomicAccount.PNL_FX_UNREALISED, PostingSide.CREDIT, "5", attributed),
        _posting(EconomicAccount.PNL_FX_REALISED, PostingSide.CREDIT, "8", shared),
        _posting(EconomicAccount.PNL_FX_UNREALISED, PostingSide.DEBIT, "3", shared),
    ]

    report = StrategyBookReporter().report(postings)
    arb = report.totals_for(StrategyBook.ARBITRAGE)
    value = report.totals_for(StrategyBook.RESEARCH_VALUE)

    assert arb.allocated_realised_fx_gbp == Decimal(10) * USD_RATE
    assert arb.allocated_unrealised_fx_gbp == Decimal(5) * USD_RATE
    assert value.allocated_realised_fx_gbp == Decimal(0)
    assert report.shared_treasury.allocated_realised_fx_gbp == Decimal(8) * USD_RATE
    assert report.shared_treasury.allocated_unrealised_fx_gbp == Decimal(-3) * USD_RATE
    assert report.shared_treasury.attribution is AttributionScope.SHARED_UNALLOCATED


def test_capital_is_reported_by_native_pool_and_optional_strategy_reservation() -> None:
    arb_mb = _dims(StrategyBook.ARBITRAGE, venue=VenueName.MATCHBOOK, currency="GBP")
    value_pm = _dims(StrategyBook.RESEARCH_VALUE, venue=VenueName.POLYMARKET, currency="USD")
    shared_sm = _dims(None, venue=VenueName.SMARKETS, currency="GBP")
    postings = [
        _posting(cash_account(VenueName.MATCHBOOK, "GBP", CashState.AVAILABLE), PostingSide.DEBIT, "1000", arb_mb),
        _posting(cash_account(VenueName.MATCHBOOK, "GBP", CashState.LOCKED), PostingSide.DEBIT, "200", arb_mb),
        _posting(
            cash_account(VenueName.POLYMARKET, "USD", CashState.AVAILABLE),
            PostingSide.DEBIT,
            "500",
            value_pm,
        ),
        _posting(
            cash_account(VenueName.SMARKETS, "GBP", CashState.TRANSIT),
            PostingSide.DEBIT,
            "50",
            shared_sm,
        ),
    ]

    report = StrategyBookReporter().report(postings)
    assert report.totals_for(StrategyBook.ARBITRAGE).capital.available_gbp == Decimal(1000)
    assert report.totals_for(StrategyBook.ARBITRAGE).capital.locked_gbp == Decimal(200)
    assert report.totals_for(StrategyBook.RESEARCH_VALUE).capital.available_gbp == Decimal(500) * USD_RATE
    assert report.shared_treasury.capital.transit_gbp == Decimal(50)

    pool_ids = {row.pool.pool_id for row in report.native_pool_balances}
    assert pool_ids == {"matchbook/GBP", "polymarket/USD", "smarkets/GBP"}
    assert {pool.pool_id for pool in WELL_KNOWN_NATIVE_POOLS} == {
        "polymarket/USD",
        "matchbook/GBP",
        "smarkets/GBP",
    }

    usd_reserved = report.native_balances_for(
        venue=VenueName.POLYMARKET,
        currency="USD",
        strategy_book=StrategyBook.RESEARCH_VALUE,
        include_shared=False,
    )
    assert len(usd_reserved) == 1
    assert usd_reserved[0].amount_native == Decimal(500)
    assert usd_reserved[0].amount_gbp == Decimal(500) * USD_RATE
    assert usd_reserved[0].capital_source is CapitalSource.AUTO_POOL


def test_auto_pool_and_manual_override_capital_are_reported_separately() -> None:
    auto = _dims(
        StrategyBook.ARBITRAGE,
        venue=VenueName.MATCHBOOK,
        currency="GBP",
        capital_source=CapitalSource.AUTO_POOL,
    )
    manual = _dims(
        StrategyBook.ARBITRAGE,
        venue=VenueName.MATCHBOOK,
        currency="GBP",
        capital_source=CapitalSource.MANUAL_OVERRIDE,
    )
    unallocated = _dims(
        None,
        venue=VenueName.MATCHBOOK,
        currency="GBP",
        capital_source=CapitalSource.SHARED_UNALLOCATED,
    )
    postings = [
        _posting(
            cash_account(VenueName.MATCHBOOK, "GBP", CashState.AVAILABLE),
            PostingSide.DEBIT,
            "800",
            auto,
        ),
        _posting(
            cash_account(VenueName.MATCHBOOK, "GBP", CashState.LOCKED),
            PostingSide.DEBIT,
            "150",
            manual,
        ),
        _posting(
            cash_account(VenueName.MATCHBOOK, "GBP", CashState.AVAILABLE),
            PostingSide.DEBIT,
            "50",
            unallocated,
        ),
    ]

    report = StrategyBookReporter().report(postings)
    arb = report.totals_for(StrategyBook.ARBITRAGE)
    assert arb.capital.available_gbp == Decimal(800)
    assert arb.capital.locked_gbp == Decimal(150)
    assert arb.capital_by_source[CapitalSource.AUTO_POOL].available_gbp == Decimal(800)
    assert arb.capital_by_source[CapitalSource.MANUAL_OVERRIDE].locked_gbp == Decimal(150)
    assert report.capital_for_source(CapitalSource.AUTO_POOL).available_gbp == Decimal(800)
    assert report.capital_for_source("MANUAL_OVERRIDE").locked_gbp == Decimal(150)
    assert report.capital_for_source("SHARED/UNALLOCATED").available_gbp == Decimal(50)
    assert report.shared_treasury.capital.available_gbp == Decimal(50)

    manual_rows = report.native_balances_for(
        venue=VenueName.MATCHBOOK,
        currency="GBP",
        strategy_book=StrategyBook.ARBITRAGE,
        capital_source=CapitalSource.MANUAL_OVERRIDE,
        include_shared=False,
    )
    assert len(manual_rows) == 1
    assert manual_rows[0].amount_native == Decimal(150)
    assert {row.capital_source for row in report.native_pool_balances} == {
        CapitalSource.AUTO_POOL,
        CapitalSource.MANUAL_OVERRIDE,
        CapitalSource.SHARED_UNALLOCATED,
    }


def test_native_amounts_cannot_be_mixed_across_currencies() -> None:
    mixed = [
        _posting(
            cash_account(VenueName.MATCHBOOK, "GBP", CashState.AVAILABLE),
            PostingSide.DEBIT,
            "10",
            _dims(StrategyBook.ARBITRAGE, venue=VenueName.MATCHBOOK, currency="GBP"),
        ),
        _posting(
            cash_account(VenueName.POLYMARKET, "USD", CashState.AVAILABLE),
            PostingSide.DEBIT,
            "10",
            _dims(StrategyBook.ARBITRAGE, venue=VenueName.POLYMARKET, currency="USD"),
        ),
    ]
    with pytest.raises(NativeCurrencyMixError, match="cannot combine native amounts"):
        sum_native(mixed)


def test_non_gbp_posting_requires_fx_layer_presentation() -> None:
    dims = _dims(StrategyBook.ARBITRAGE, venue=VenueName.POLYMARKET, currency="USD")
    with pytest.raises(ValidationError, match="accounting FX layer"):
        DimensionedPosting(
            account_code=EconomicAccount.PNL_BETTING,
            side=PostingSide.CREDIT,
            amount_native=Decimal(10),
            amount_gbp=Decimal(10),
            fx_rate_gbp_per_unit=None,
            dimensions=dims,
        )
    error = missing_gbp_presentation_error("USD")
    assert "cannot be treated as GBP" in str(error)


def test_reporter_does_not_create_a_mutable_balance_store() -> None:
    dims = _dims(StrategyBook.ARBITRAGE, venue=VenueName.MATCHBOOK, currency="GBP")
    reporter = StrategyBookReporter()
    first = reporter.report(
        [_posting(EconomicAccount.PNL_BETTING, PostingSide.CREDIT, "5", dims)]
    )
    second = reporter.report(
        [_posting(EconomicAccount.PNL_BETTING, PostingSide.CREDIT, "7", dims)]
    )
    assert first.totals_for(StrategyBook.ARBITRAGE).realised_betting_pnl_gbp == Decimal(5)
    assert second.totals_for(StrategyBook.ARBITRAGE).realised_betting_pnl_gbp == Decimal(7)
    assert not hasattr(reporter, "balances")
    assert not hasattr(reporter, "_ledger")
