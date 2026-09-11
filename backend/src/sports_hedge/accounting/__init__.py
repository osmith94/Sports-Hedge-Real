"""Accounting dimensions and profit-centre reporting.

This package is intentionally a tagging and reporting layer over a single
append-only ledger. It does not store mutable balances and is designed so the
FX ledger foundation can attach `PostingDimensions` to journals later.
"""

from sports_hedge.accounting.dimensions import (
    AttributionScope,
    CashState,
    EconomicAccount,
    NativeLiquidityPool,
    PostingDimensions,
    PostingSide,
    ProductModule,
    StrategyBook,
    UnknownStrategyBookError,
    parse_strategy_book,
    product_module_for,
)
from sports_hedge.accounting.strategy_books import (
    CapitalBucketTotals,
    DimensionedPosting,
    NativeCurrencyMixError,
    NativePoolBalance,
    ProfitCentreTotals,
    StrategyBookReport,
    StrategyBookReporter,
    missing_gbp_presentation_error,
)

__all__ = [
    "AttributionScope",
    "CapitalBucketTotals",
    "CashState",
    "DimensionedPosting",
    "EconomicAccount",
    "NativeCurrencyMixError",
    "NativeLiquidityPool",
    "NativePoolBalance",
    "PostingDimensions",
    "PostingSide",
    "ProductModule",
    "ProfitCentreTotals",
    "StrategyBook",
    "StrategyBookReport",
    "StrategyBookReporter",
    "UnknownStrategyBookError",
    "missing_gbp_presentation_error",
    "parse_strategy_book",
    "product_module_for",
]
