"""Non-blocking emission of accounting domain events from the operational path.

Reporting failure or a stale event store must never raise into scanner, Treasury
or paper-trading code. This module never calls venue providers.
"""

from __future__ import annotations

import logging
from typing import Any

from sports_hedge.accounting.adapters import (
    from_journal_entry,
    from_trade_audit,
    from_treasury_event,
)
from sports_hedge.accounting.events import AccountingDomainEvent, DuplicateAccountingEventError
from sports_hedge.accounting.paper_journal import PaperJournalEntry
from sports_hedge.paper.trades import PaperTrade, PaperTradeAuditEvent
from sports_hedge.treasury.models import PaperTreasuryEvent

LOGGER = logging.getLogger(__name__)

# Phase 1: accounting never places, cancels or signs venue orders.
PROVIDER_WRITES_FORBIDDEN = True


class AccountingEventEmitter:
    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        self.last_error: str | None = None
        self.last_emit_ok: bool = True

    def emit(self, event: AccountingDomainEvent | None) -> bool:
        if event is None:
            return True
        if self.store is None:
            self.last_error = "accounting_event_store_unavailable"
            self.last_emit_ok = False
            return False
        try:
            self.store.append_idempotent(event)
        except DuplicateAccountingEventError as exc:
            self.last_error = str(exc)
            self.last_emit_ok = False
            LOGGER.warning("accounting event conflict ignored operationally: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001 — operational path must not block
            self.last_error = str(exc)
            self.last_emit_ok = False
            LOGGER.warning("accounting event emit failed; scanning continues: %s", exc)
            return False
        self.last_error = None
        self.last_emit_ok = True
        return True

    def emit_treasury(self, event: PaperTreasuryEvent, **kwargs: Any) -> bool:
        try:
            mapped = from_treasury_event(event, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.last_emit_ok = False
            LOGGER.warning("accounting treasury adapter failed; scanning continues: %s", exc)
            return False
        return self.emit(mapped)

    def emit_journal(self, entry: PaperJournalEntry) -> bool:
        try:
            mapped = from_journal_entry(entry)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.last_emit_ok = False
            LOGGER.warning("accounting journal adapter failed; scanning continues: %s", exc)
            return False
        return self.emit(mapped)

    def emit_trade_audit(self, trade: PaperTrade, event: PaperTradeAuditEvent) -> bool:
        try:
            mapped = from_trade_audit(trade, event)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.last_emit_ok = False
            LOGGER.warning("accounting trade adapter failed; scanning continues: %s", exc)
            return False
        return self.emit(mapped)
