"""Adapt existing Treasury / journal / trade facts into accounting domain events.

Does not replace operational ledgers. Maps durable paper facts already written
by scanner/trading/Treasury into the #476 event contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from sports_hedge.accounting.dimensions import (
    AttributionScope,
    CapitalSource,
    CashState,
    StrategyBook,
    parse_capital_source,
)
from sports_hedge.accounting.events import (
    AccountingDomainEvent,
    AccountingEventPayload,
    AccountingEventType,
    domain_event,
)
from sports_hedge.accounting.paper_journal import DataProvenance, PaperJournalEntry
from sports_hedge.accounting.reconciliation import FINANCIAL_JOURNAL_SOURCES
from sports_hedge.accounting.revaluation import REVALUATION_SOURCE
from sports_hedge.paper.trades import PaperTrade, PaperTradeAuditEvent, PaperTradeAuditEventType
from sports_hedge.treasury.models import PaperTreasuryEvent, PaperTreasuryEventType

# Journal sources whose cash movement is already represented by treasury events.
TREASURY_BACKED_JOURNAL_SOURCES = FINANCIAL_JOURNAL_SOURCES - {
    REVALUATION_SOURCE,
    "paper_treasury_transfer",
}

_TRADE_FILL_TYPES = {
    PaperTradeAuditEventType.FILLS_RECORDED,
    PaperTradeAuditEventType.PAPER_AUTOFILL,
    PaperTradeAuditEventType.PAPER_SIMULATED_EXTERNAL_FILL,
    PaperTradeAuditEventType.MANUAL_EXTERNAL_CONFIRMED,
}

_SKIP_TREASURY_TYPES = {
    PaperTreasuryEventType.SESSION_OPEN,
    PaperTreasuryEventType.SESSION_CLOSE,
}


def from_treasury_event(
    event: PaperTreasuryEvent,
    *,
    journal: PaperJournalEntry | None = None,
    capital_source: str | CapitalSource | None = None,
    provenance: DataProvenance | str | None = None,
) -> AccountingDomainEvent | None:
    """Map one operational treasury event to a domain event, or None if not in-contract."""

    event_type = _treasury_event_type(event)
    if event_type is None:
        return None
    dims = _dimensions_from_journal(journal)
    default_capital = (
        CapitalSource.AUTO_POOL
        if event_type
        in {
            AccountingEventType.PAPER_CAPITAL_LOCK,
            AccountingEventType.PAPER_CAPITAL_RELEASE,
            AccountingEventType.SETTLEMENT_PNL,
            AccountingEventType.FEE_POSTED,
        }
        else CapitalSource.SHARED_UNALLOCATED
    )
    source_capital = (
        parse_capital_source(capital_source)
        if capital_source is not None
        else dims.get("capital_source", default_capital)
    )
    attribution, strategy_book = _attribution_for(event_type, dims)
    rate = event.fx_rate_gbp_per_unit
    if event_type in {AccountingEventType.SETTLEMENT_PNL, AccountingEventType.MANUAL_ADJUSTMENT}:
        amount_native = event.native_amount
    else:
        amount_native = abs(event.native_amount)
    amount_gbp = amount_native * rate if rate is not None else Decimal("0")
    payload = AccountingEventPayload(
        venue=event.venue,
        currency=event.native_currency,
        amount_native=amount_native,
        amount_gbp=amount_gbp,
        fx_rate_gbp_per_unit=rate if rate and rate > 0 else None,
        fx_source=event.fx_source,
        capital_source=source_capital,
        attribution=attribution,
        strategy_book=strategy_book,
        cash_state=_cash_state_for(event_type),
        trade_id=event.trade_id,
        opportunity_id=event.opportunity_id,
        lock_id=event.lock_id,
        session_id=event.session_id,
        pool_id=event.pool_id,
        reason=event.reason,
        adjustment_kind=_adjustment_kind(event),
        operator_reference=(
            event.source_id if event_type is AccountingEventType.MANUAL_ADJUSTMENT else None
        ),
        journal_id=event.journal_id,
    )
    if event_type is AccountingEventType.FX_REVALUATION:
        payload.fx_source = event.fx_source or "treasury_fx_carrying_snapshot"
        payload.fx_rate_gbp_per_unit = rate if rate and rate > 0 else Decimal("1")
        payload.fx_status = "source_fact"
        payload.amount_native = Decimal("0")
        payload.amount_gbp = Decimal("0")
    return domain_event(
        event_type=event_type,
        source=event.source,
        source_id=event.source_id,
        occurred_at=event.occurred_at,
        payload=payload,
        provenance=_provenance(provenance),
    )


def from_journal_entry(entry: PaperJournalEntry) -> AccountingDomainEvent | None:
    """Map journal-only facts (FX revaluation, transfers) that treasury does not emit."""

    if entry.source in TREASURY_BACKED_JOURNAL_SOURCES:
        return None
    if entry.source == REVALUATION_SOURCE:
        return _from_revaluation_journal(entry)
    if entry.source == "paper_treasury_transfer":
        return _from_transfer_journal(entry)
    return None


def from_trade_audit(trade: PaperTrade, event: PaperTradeAuditEvent) -> AccountingDomainEvent | None:
    if event.event_type is PaperTradeAuditEventType.TRADE_OPENED:
        venue = trade.legs[0].venue if trade.legs else None
        currency = trade.legs[0].currency if trade.legs else None
        return domain_event(
            event_type=AccountingEventType.PAPER_TRADE_OPENED,
            source="paper_trade",
            source_id=event.event_id,
            occurred_at=event.occurred_at,
            payload=AccountingEventPayload(
                venue=venue,
                currency=currency,
                trade_id=trade.trade_id,
                opportunity_id=trade.opportunity_id,
                reason=event.detail or "paper trade opened",
                attribution=AttributionScope.STRATEGY,
                strategy_book=StrategyBook.ARBITRAGE,
                capital_source=CapitalSource.AUTO_POOL,
            ),
            provenance=_trade_provenance(trade),
        )
    if event.event_type in _TRADE_FILL_TYPES:
        if not trade.legs:
            return None
        venue = trade.legs[0].venue if trade.legs else None
        currency = trade.legs[0].currency if trade.legs else "GBP"
        fill_kind = trade.legs[0].fill_kind.value if trade.legs else None
        capital = trade.legs[0].capital_source if trade.legs else CapitalSource.AUTO_POOL
        return domain_event(
            event_type=AccountingEventType.PAPER_TRADE_FILL,
            source="paper_trade",
            source_id=event.event_id,
            occurred_at=event.occurred_at,
            payload=AccountingEventPayload(
                venue=venue,
                currency=currency or "GBP",
                trade_id=trade.trade_id,
                opportunity_id=trade.opportunity_id,
                reason=event.detail or event.event_type.value,
                fill_kind=fill_kind,
                attribution=AttributionScope.STRATEGY,
                strategy_book=StrategyBook.ARBITRAGE,
                capital_source=capital,
            ),
            provenance=DataProvenance.LIVE_PAPER,
        )
    return None


def collect_operational_events(ledger: Any) -> list[AccountingDomainEvent]:
    """Rebuild the event stream from durable Treasury / journal / trade facts."""

    events: list[AccountingDomainEvent] = []
    journal_by_id = {entry.journal_id: entry for entry in ledger.journal.list_entries()}
    treasury_events = list(reversed(ledger.treasury.list_events(limit=10_000)))
    for item in treasury_events:
        journal = journal_by_id.get(item.journal_id) if item.journal_id else None
        mapped = from_treasury_event(item, journal=journal)
        if mapped is not None:
            events.append(mapped)
    seen = {(event.source, event.source_id) for event in events}
    for entry in ledger.journal.list_entries():
        if (entry.source, entry.source_id) in seen:
            continue
        mapped = from_journal_entry(entry)
        if mapped is not None:
            events.append(mapped)
            seen.add((mapped.source, mapped.source_id))
    for trade in _list_trades(ledger):
        for item in trade.audit:
            mapped = from_trade_audit(trade, item)
            if mapped is None:
                continue
            if (mapped.source, mapped.source_id) in seen:
                continue
            events.append(mapped)
            seen.add((mapped.source, mapped.source_id))
    events.sort(key=lambda event: (event.occurred_at, event.event_id))
    return events


def hydrate_event_store(ledger: Any) -> list[AccountingDomainEvent]:
    """Idempotently append adapted operational facts into the accounting event store."""

    from sports_hedge.accounting.events import DuplicateAccountingEventError

    store = ledger.accounting_events
    for event in collect_operational_events(ledger):
        try:
            store.append_idempotent(event)
        except DuplicateAccountingEventError:
            continue
    return store.list_in_order()


def _treasury_event_type(event: PaperTreasuryEvent) -> AccountingEventType | None:
    if event.event_type in _SKIP_TREASURY_TYPES:
        return None
    mapping = {
        PaperTreasuryEventType.SEED: AccountingEventType.CAPITAL_INTRODUCED,
        PaperTreasuryEventType.LOCK: AccountingEventType.PAPER_CAPITAL_LOCK,
        PaperTreasuryEventType.RELEASE: AccountingEventType.PAPER_CAPITAL_RELEASE,
        PaperTreasuryEventType.REALISED_PNL: AccountingEventType.SETTLEMENT_PNL,
        PaperTreasuryEventType.FEE: AccountingEventType.FEE_POSTED,
        PaperTreasuryEventType.CORRECTION: AccountingEventType.MANUAL_ADJUSTMENT,
        PaperTreasuryEventType.FX_CARRYING_SNAPSHOT: AccountingEventType.FX_REVALUATION,
    }
    return mapping.get(event.event_type)


def _adjustment_kind(event: PaperTreasuryEvent) -> str | None:
    if event.event_type is PaperTreasuryEventType.SEED:
        return "introduced"
    if event.event_type is PaperTreasuryEventType.CORRECTION:
        if event.native_amount < 0:
            return "withdrawn"
        if event.native_amount > 0:
            return "introduced"
        return "correction"
    return None


def _cash_state_for(event_type: AccountingEventType) -> CashState | None:
    if event_type in {
        AccountingEventType.CAPITAL_INTRODUCED,
        AccountingEventType.CAPITAL_WITHDRAWN,
        AccountingEventType.MANUAL_ADJUSTMENT,
        AccountingEventType.SETTLEMENT_PNL,
        AccountingEventType.FEE_POSTED,
    }:
        return CashState.AVAILABLE
    if event_type is AccountingEventType.PAPER_CAPITAL_LOCK:
        return CashState.LOCKED
    if event_type is AccountingEventType.PAPER_CAPITAL_RELEASE:
        return CashState.LOCKED
    if event_type is AccountingEventType.TREASURY_TRANSFER:
        return CashState.TRANSIT
    return None


_STRATEGY_EVENT_TYPES = {
    AccountingEventType.PAPER_CAPITAL_LOCK,
    AccountingEventType.PAPER_CAPITAL_RELEASE,
    AccountingEventType.SETTLEMENT_PNL,
    AccountingEventType.FEE_POSTED,
    AccountingEventType.PAPER_TRADE_OPENED,
    AccountingEventType.PAPER_TRADE_FILL,
}


def _attribution_for(
    event_type: AccountingEventType, dims: dict[str, Any]
) -> tuple[AttributionScope, StrategyBook | None]:
    if "attribution" in dims:
        attribution = dims["attribution"]
        strategy_book = dims.get("strategy_book")
        if attribution is AttributionScope.STRATEGY and strategy_book is None:
            strategy_book = StrategyBook.ARBITRAGE
        if attribution is AttributionScope.SHARED_UNALLOCATED:
            strategy_book = None
        return attribution, strategy_book
    if event_type in _STRATEGY_EVENT_TYPES:
        return AttributionScope.STRATEGY, StrategyBook.ARBITRAGE
    return AttributionScope.SHARED_UNALLOCATED, None


def _dimensions_from_journal(entry: PaperJournalEntry | None) -> dict[str, Any]:
    if entry is None or not entry.postings:
        return {}
    dims = entry.postings[0].dimensions
    return {
        "capital_source": dims.capital_source,
        "attribution": dims.attribution,
        "strategy_book": dims.strategy_book,
    }


def _provenance(value: DataProvenance | str | None) -> DataProvenance:
    if isinstance(value, DataProvenance):
        return value
    if isinstance(value, str) and value:
        return DataProvenance(value)
    return DataProvenance.LIVE_PAPER


def _trade_provenance(trade: PaperTrade) -> DataProvenance:
    value = getattr(trade, "provenance", None)
    if isinstance(value, DataProvenance):
        return value
    if isinstance(value, str) and value:
        try:
            return DataProvenance(value)
        except ValueError:
            return DataProvenance.LIVE_PAPER
    return DataProvenance.LIVE_PAPER


def _from_revaluation_journal(entry: PaperJournalEntry) -> AccountingDomainEvent:
    posting = entry.postings[0]
    dims = posting.dimensions
    signed = posting.amount_gbp if posting.side.value == "debit" else -posting.amount_gbp
    return domain_event(
        event_type=AccountingEventType.FX_REVALUATION,
        source=entry.source,
        source_id=entry.source_id,
        occurred_at=entry.occurred_at,
        payload=AccountingEventPayload(
            venue=dims.venue,
            currency=dims.currency,
            amount_native=Decimal("0"),
            amount_gbp=signed,
            fx_rate_gbp_per_unit=posting.fx_rate_gbp_per_unit,
            fx_source="ecb_eurofxref",
            fx_status="valuation",
            capital_source=dims.capital_source,
            attribution=dims.attribution,
            strategy_book=dims.strategy_book,
            opportunity_id=entry.opportunity_id,
            reason=entry.description,
            journal_id=entry.journal_id,
        ),
        provenance=entry.provenance,
    )


def _from_transfer_journal(entry: PaperJournalEntry) -> AccountingDomainEvent:
    source_posting = next(item for item in entry.postings if item.side.value == "credit")
    dest_posting = next(item for item in entry.postings if item.side.value == "debit")
    return domain_event(
        event_type=AccountingEventType.TREASURY_TRANSFER,
        source=entry.source,
        source_id=entry.source_id,
        occurred_at=entry.occurred_at,
        payload=AccountingEventPayload(
            venue=source_posting.dimensions.venue,
            currency=source_posting.dimensions.currency,
            amount_native=source_posting.amount_native,
            amount_gbp=source_posting.amount_gbp,
            fx_rate_gbp_per_unit=source_posting.fx_rate_gbp_per_unit,
            counterparty_venue=dest_posting.dimensions.venue,
            counterparty_currency=dest_posting.dimensions.currency,
            counterparty_amount_native=dest_posting.amount_native,
            counterparty_amount_gbp=dest_posting.amount_gbp,
            capital_source=source_posting.dimensions.capital_source,
            attribution=source_posting.dimensions.attribution,
            strategy_book=source_posting.dimensions.strategy_book,
            cash_state=CashState.TRANSIT,
            opportunity_id=entry.opportunity_id,
            reason=entry.description,
            journal_id=entry.journal_id,
        ),
        provenance=entry.provenance,
    )


def _list_trades(ledger: Any) -> Iterable[PaperTrade]:
    trades = getattr(ledger, "trades", None)
    if trades is None:
        return []
    return trades.list_all()
