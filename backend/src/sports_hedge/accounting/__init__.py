"""Accounting dimensions and profit-centre reporting.

This package is intentionally a tagging and reporting layer over a single
append-only ledger. It does not store mutable balances and is designed so the
FX ledger foundation can attach `PostingDimensions` to journals later.
"""

from sports_hedge.accounting.dimensions import (
    AttributionScope,
    CapitalSource,
    CashState,
    EconomicAccount,
    NativeLiquidityPool,
    PostingDimensions,
    PostingSide,
    ProductModule,
    StrategyBook,
    UnknownCapitalSourceError,
    UnknownStrategyBookError,
    parse_capital_source,
    parse_strategy_book,
    product_module_for,
)
from sports_hedge.accounting.reconciliation import (
    LedgerReconciliationError,
    PaperLedgerReconciliation,
    reconcile_paper_ledger,
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
    "CapitalSource",
    "CashState",
    "DimensionedPosting",
    "EconomicAccount",
    "LedgerReconciliationError",
    "NativeCurrencyMixError",
    "NativeLiquidityPool",
    "NativePoolBalance",
    "PaperLedgerReconciliation",
    "PostingDimensions",
    "PostingSide",
    "ProductModule",
    "ProfitCentreTotals",
    "StrategyBook",
    "StrategyBookReport",
    "StrategyBookReporter",
    "UnknownCapitalSourceError",
    "UnknownStrategyBookError",
    "missing_gbp_presentation_error",
    "parse_capital_source",
    "parse_strategy_book",
    "product_module_for",
    "reconcile_paper_ledger",
]
