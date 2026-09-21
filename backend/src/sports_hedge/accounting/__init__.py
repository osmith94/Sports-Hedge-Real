"""Accounting dimensions, profit-centre reporting, and CQRS event projections.

Operational scanner/trading/Treasury paths emit small immutable domain events.
GL, balance-sheet, reconciliation and management-reporting projections are
rebuilt on demand from that stream. This package does not store mutable
balances and never writes to venue providers.
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
from sports_hedge.accounting.events import (
    EVENT_SCHEMA_VERSION,
    AccountingDomainEvent,
    AccountingEventType,
    DuplicateAccountingEventError,
)
from sports_hedge.accounting.projections import (
    PROJECTION_VERSION,
    AccountingProjectionBundle,
    AccountingProjectionService,
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
    "EVENT_SCHEMA_VERSION",
    "PROJECTION_VERSION",
    "AccountingDomainEvent",
    "AccountingEventType",
    "AccountingProjectionBundle",
    "AccountingProjectionService",
    "AttributionScope",
    "CapitalBucketTotals",
    "CapitalSource",
    "CashState",
    "DimensionedPosting",
    "DuplicateAccountingEventError",
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
