"""Reconcile native paper treasury balances to append-only journal facts.

This is the Lane 5 financial-truth seam. It is not a Finance UI and not a
production general ledger. Native USD and GBP amounts are never summed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.accounting.dimensions import CashState, EconomicAccount, PostingSide
from sports_hedge.accounting.strategy_books import (
    DimensionedPosting,
    classify_pnl_account,
    parse_cash_account,
)
from sports_hedge.domain.models import VenueName

_ZERO = Decimal("0")

FINANCIAL_JOURNAL_SOURCES = frozenset(
    {
        "paper_treasury_seed",
        "paper_treasury_adjust",
        "paper_fill_simulator",
        "paper_settlement",
        "paper_unwind",
        "paper_demo_reset",
        "manual_external_confirmation",
        "daily_fx_revaluation",
    }
)

OPERATIONAL_NOT_GL_SOURCES = frozenset(
    {
        "paper_scan",
        "watchlist",
        "gpt",
        "llm",
        "openai",
        "decision_log",
        "opportunity_log",
    }
)

TREASURY_EVENTS_REQUIRING_JOURNAL = frozenset(
    {
        "seed",
        "lock",
        "release",
        "realised_pnl",
        "fee",
        "correction",
    }
)


class LedgerReconciliationError(ValueError):
    """Raised when treasury native facts do not match the append-only journal."""


class PaperLedgerReconciliation(BaseModel):
    """Point-in-time proof that journal facts and treasury pools agree."""

    journal_count: int = Field(ge=0)
    treasury_event_count: int = Field(ge=0)
    unique_journal_source_ids: bool
    unique_treasury_source_ids: bool
    gbp_journals_balanced: bool
    native_available: dict[str, str]
    native_locked: dict[str, str]
    native_realised_pnl: dict[str, str]
    native_fees: dict[str, str]
    mismatches: list[str] = Field(default_factory=list)
    data_kind: str = "persisted_paper_subledger"

    @property
    def ok(self) -> bool:
        return (
            self.unique_journal_source_ids
            and self.unique_treasury_source_ids
            and self.gbp_journals_balanced
            and not self.mismatches
        )


def journal_native_cash(
    postings: Iterable[DimensionedPosting],
) -> dict[tuple[VenueName, str, CashState], Decimal]:
    balances: dict[tuple[VenueName, str, CashState], Decimal] = defaultdict(lambda: _ZERO)
    for posting in postings:
        parsed = parse_cash_account(posting.account_code)
        if parsed is None:
            continue
        state, venue, currency = parsed
        balances[(venue, currency, state)] += posting.signed_native
    return dict(balances)


def journal_native_pnl(
    postings: Iterable[DimensionedPosting],
) -> tuple[dict[tuple[VenueName, str], Decimal], dict[tuple[VenueName, str], Decimal]]:
    """Credit-positive betting P&L and debit-positive venue fees, native units."""

    betting: dict[tuple[VenueName, str], Decimal] = defaultdict(lambda: _ZERO)
    fees: dict[tuple[VenueName, str], Decimal] = defaultdict(lambda: _ZERO)
    for posting in postings:
        account = classify_pnl_account(posting.account_code)
        venue = posting.dimensions.venue
        if venue is None or account is None:
            continue
        key = (venue, posting.dimensions.currency)
        if account is EconomicAccount.PNL_BETTING:
            betting[key] -= posting.signed_native
        elif account is EconomicAccount.PNL_VENUE_FEES:
            fees[key] += posting.signed_native
    return dict(betting), dict(fees)


def _source_keys(rows: Iterable[tuple[str, str]]) -> tuple[bool, list[str]]:
    seen: set[tuple[str, str]] = set()
    duplicates: list[str] = []
    for source, source_id in rows:
        key = (source, source_id)
        if key in seen:
            duplicates.append(f"{source}:{source_id}")
        seen.add(key)
    return not duplicates, duplicates


def reconcile_paper_ledger(ledger: Any) -> PaperLedgerReconciliation:
    """Compare treasury pool rows to journal cash/P&L facts.

    Raises LedgerReconciliationError when native cash, locks, fees or P&L
    cannot be reconstructed from the append-only journal.
    """

    entries = ledger.journal.list_entries()
    snapshot = ledger.treasury.snapshot(event_limit=10_000)
    events = ledger.treasury.list_events(
        limit=10_000,
        session_id=snapshot.session.session_id if snapshot.session else None,
    )

    journal_keys = [(entry.source, entry.source_id) for entry in entries]
    unique_journals, duplicate_journals = _source_keys(journal_keys)
    treasury_keys = [(event.source, event.source_id) for event in events]
    unique_events, duplicate_events = _source_keys(treasury_keys)

    mismatches: list[str] = []
    if not unique_journals:
        mismatches.append(f"duplicate_journal_source_ids:{sorted(duplicate_journals)}")
    if not unique_events:
        mismatches.append(f"duplicate_treasury_source_ids:{sorted(duplicate_events)}")

    gbp_balanced = True
    for entry in entries:
        debits = sum(
            (item.amount_gbp for item in entry.postings if item.side is PostingSide.DEBIT),
            _ZERO,
        )
        credits = sum(
            (item.amount_gbp for item in entry.postings if item.side is PostingSide.CREDIT),
            _ZERO,
        )
        if debits != credits:
            gbp_balanced = False
            mismatches.append(
                f"unbalanced_gbp_journal:{entry.source}:{entry.source_id} debit={debits} credit={credits}"
            )

    for entry in entries:
        if entry.source in OPERATIONAL_NOT_GL_SOURCES:
            mismatches.append(f"operational_source_in_gl:{entry.source}:{entry.source_id}")
        elif entry.source not in FINANCIAL_JOURNAL_SOURCES and "gpt" in entry.source.lower():
            mismatches.append(f"gpt_interpretation_in_gl:{entry.source}:{entry.source_id}")
        lowered = f"{entry.source} {entry.description}".lower()
        if any(token in lowered for token in ("gpt", "openai", "llm interpretation")):
            mismatches.append(f"gpt_interpretation_in_gl:{entry.source}:{entry.source_id}")

    linked_ids = {event.journal_id for event in events if event.journal_id}
    session_entries = [entry for entry in entries if entry.journal_id in linked_ids] if linked_ids else []
    session_postings: list[DimensionedPosting] = []
    for entry in session_entries:
        for index, posting in enumerate(entry.postings):
            session_postings.append(
                posting.as_dimensioned(
                    journal_id=entry.journal_id,
                    posting_id=f"{entry.journal_id}:{index}",
                )
            )
    cash = journal_native_cash(session_postings)
    betting, fees = journal_native_pnl(session_postings)
    native_available: dict[str, str] = {}
    native_locked: dict[str, str] = {}
    native_pnl: dict[str, str] = {}
    native_fees: dict[str, str] = {}

    for pool in snapshot.pools:
        available_key = (pool.venue, pool.native_currency, CashState.AVAILABLE)
        locked_key = (pool.venue, pool.native_currency, CashState.LOCKED)
        transit_key = (pool.venue, pool.native_currency, CashState.TRANSIT)
        journal_available = cash.get(available_key, _ZERO)
        journal_locked = cash.get(locked_key, _ZERO)
        journal_transit = cash.get(transit_key, _ZERO)
        identity = f"{pool.venue.value}/{pool.native_currency}"
        native_available[identity] = str(journal_available)
        native_locked[identity] = str(journal_locked)
        if journal_available != pool.available_cash:
            mismatches.append(
                f"available_mismatch:{identity} journal={journal_available} treasury={pool.available_cash}"
            )
        if journal_locked != pool.locked_capital:
            mismatches.append(
                f"locked_mismatch:{identity} journal={journal_locked} treasury={pool.locked_capital}"
            )
        if journal_transit != _ZERO:
            mismatches.append(f"unexpected_transit:{identity}={journal_transit}")
        pnl_key = (pool.venue, pool.native_currency)
        journal_betting = betting.get(pnl_key, _ZERO)
        journal_fee = fees.get(pnl_key, _ZERO)
        net_pnl = journal_betting - journal_fee
        native_pnl[identity] = str(net_pnl)
        native_fees[identity] = str(journal_fee)
        if journal_fee != pool.cumulative_fees_native:
            mismatches.append(
                f"fee_mismatch:{identity} journal={journal_fee} treasury={pool.cumulative_fees_native}"
            )
        if net_pnl != pool.realised_pnl_native:
            mismatches.append(
                f"pnl_mismatch:{identity} journal_net={net_pnl} treasury={pool.realised_pnl_native}"
            )

    pool_identities = {f"{item.venue.value}/{item.native_currency}" for item in snapshot.pools}
    for (venue, currency, state), amount in cash.items():
        identity = f"{venue.value}/{currency}"
        if identity not in pool_identities and amount != _ZERO:
            mismatches.append(f"journal_cash_without_pool:{identity}/{state.value}={amount}")

    session = snapshot.session
    if session is not None:
        lock_rows = ledger._connection.execute(
            """
            SELECT venue, native_currency, locked_native, released_native, status, lock_id, trade_id
            FROM paper_treasury_locks
            WHERE session_id = ?
            """,
            (session.session_id,),
        ).fetchall()
        remaining_by_pool: dict[tuple[str, str], Decimal] = defaultdict(lambda: _ZERO)
        for row in lock_rows:
            remaining = Decimal(row["locked_native"]) - Decimal(row["released_native"])
            if remaining < 0:
                mismatches.append(f"negative_lock_remaining:{row['lock_id']}")
                continue
            if remaining > 0 and row["status"] != "open":
                mismatches.append(f"open_amount_on_released_lock:{row['lock_id']}")
            if remaining == 0 and row["status"] == "open":
                mismatches.append(f"zero_remaining_still_open:{row['lock_id']}")
            remaining_by_pool[(row["venue"], row["native_currency"])] += remaining
        for pool in snapshot.pools:
            remaining = remaining_by_pool.get((pool.venue.value, pool.native_currency), _ZERO)
            if remaining != pool.locked_capital:
                mismatches.append(
                    f"lock_rows_mismatch:{pool.venue.value}/{pool.native_currency} "
                    f"locks={remaining} pool={pool.locked_capital}"
                )

    journal_ids = {entry.journal_id for entry in entries}
    for event in events:
        if event.event_type.value not in TREASURY_EVENTS_REQUIRING_JOURNAL:
            continue
        if not event.journal_id:
            mismatches.append(f"treasury_event_missing_journal:{event.source}:{event.source_id}")
        elif event.journal_id not in journal_ids:
            mismatches.append(
                f"treasury_event_unknown_journal:{event.source}:{event.source_id}:{event.journal_id}"
            )

    report = PaperLedgerReconciliation(
        journal_count=len(entries),
        treasury_event_count=len(events),
        unique_journal_source_ids=unique_journals,
        unique_treasury_source_ids=unique_events,
        gbp_journals_balanced=gbp_balanced,
        native_available=native_available,
        native_locked=native_locked,
        native_realised_pnl=native_pnl,
        native_fees=native_fees,
        mismatches=mismatches,
    )
    if not report.ok:
        raise LedgerReconciliationError("; ".join(report.mismatches) or "ledger_reconciliation_failed")
    return report
