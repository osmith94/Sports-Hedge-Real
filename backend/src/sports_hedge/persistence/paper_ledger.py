"""SQLite paper trade book and append-only paper subledger."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from sqlite3 import IntegrityError
from typing import Any, Iterator

from sports_hedge.accounting.paper_journal import (
    DuplicateJournalError,
    PaperJournal,
    PaperJournalEntry,
)
from sports_hedge.accounting.strategy_books import DimensionedPosting
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.trades import (
    PaperCloseFill,
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeLeg,
    PaperTradeState,
)
from sports_hedge.paper.risk_snapshot import PaperExecutionRiskSnapshot

LOGGER = logging.getLogger(__name__)


class SerializedLedgerBound:
    """Serialize public methods against the parent ledger lock.

    sqlite3.Connection is not safe for concurrent use, even with
    ``check_same_thread=False``. FastAPI runs sync paper-trade endpoints in a
    thread pool, so summary + list-active must not share an unprotected
    connection. RLock keeps nested journal/treasury transactions atomic.
    """

    def __getattribute__(self, name: str) -> Any:
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or not callable(attr):
            return attr
        ledger = object.__getattribute__(self, "_ledger")

        def bound(*args: Any, **kwargs: Any) -> Any:
            with ledger.exclusive():
                return attr(*args, **kwargs)

        return bound


class SqlitePaperJournal(SerializedLedgerBound):
    """Durable wrap of the in-memory PaperJournal contract."""

    def __init__(self, ledger: "SqlitePaperLedger") -> None:
        self._ledger = ledger
        self._connection = ledger._connection
        self._memory = PaperJournal()
        self._hydrate_memory()

    def _hydrate_memory(self) -> None:
        rows = self._connection.execute(
            "SELECT entry_json FROM paper_journal_entries ORDER BY rowid"
        ).fetchall()
        for row in rows:
            entry = PaperJournalEntry.model_validate(json.loads(row["entry_json"]))
            try:
                self._memory.append(entry)
            except DuplicateJournalError:
                continue

    def append(self, entry: PaperJournalEntry) -> PaperJournalEntry:
        posted = self._memory.append(entry)
        try:
            self._connection.execute(
                """
                INSERT INTO paper_journal_entries (
                    journal_id, source, source_id, opportunity_id, trade_id,
                    occurred_at, provenance, entry_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    posted.journal_id,
                    posted.source,
                    posted.source_id,
                    posted.opportunity_id,
                    posted.trade_id,
                    posted.occurred_at.isoformat(),
                    posted.provenance.value,
                    json.dumps(posted.model_dump(mode="json")),
                ),
            )
            self._ledger._commit()
        except IntegrityError as exc:
            raise DuplicateJournalError(
                f"duplicate journal {entry.source}:{entry.source_id}"
            ) from exc
        return posted

    def get(self, source: str, source_id: str) -> PaperJournalEntry | None:
        return self._memory.get(source, source_id)

    def append_idempotent(self, entry: PaperJournalEntry) -> tuple[PaperJournalEntry, bool]:
        existing = self.get(entry.source, entry.source_id)
        if existing is not None:
            if not existing.facts_match(entry):
                raise DuplicateJournalError(
                    f"conflicting_journal_facts {entry.source}:{entry.source_id}"
                )
            return existing, False
        return self.append(entry), True

    def list_entries(self, *, opportunity_id: str | None = None) -> list[PaperJournalEntry]:
        return self._memory.list_entries(opportunity_id=opportunity_id)

    def postings(self, *, opportunity_id: str | None = None) -> list[DimensionedPosting]:
        return self._memory.postings(opportunity_id=opportunity_id)


class SqlitePaperTradeRepository(SerializedLedgerBound):
    def __init__(self, ledger: "SqlitePaperLedger") -> None:
        self._ledger = ledger
        self._connection = ledger._connection

    def get(self, trade_id: str) -> PaperTrade | None:
        row = self._connection.execute(
            "SELECT * FROM paper_trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        if row is None:
            return None
        return self._trade_from_row(row)

    def get_by_opportunity(self, opportunity_id: str) -> PaperTrade | None:
        row = self._connection.execute(
            "SELECT * FROM paper_trades WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()
        if row is None:
            return None
        return self._trade_from_row(row)

    def list_active(self) -> list[PaperTrade]:
        rows = self._connection.execute(
            "SELECT * FROM paper_trades WHERE state != ? ORDER BY opened_at DESC",
            (PaperTradeState.CLOSED.value,),
        ).fetchall()
        return self._trades_from_rows(rows)

    def list_closed(self) -> list[PaperTrade]:
        rows = self._connection.execute(
            "SELECT * FROM paper_trades WHERE state = ? ORDER BY settled_at DESC, opened_at DESC",
            (PaperTradeState.CLOSED.value,),
        ).fetchall()
        return self._trades_from_rows(rows)

    def list_all(self) -> list[PaperTrade]:
        rows = self._connection.execute("SELECT * FROM paper_trades ORDER BY opened_at DESC").fetchall()
        return self._trades_from_rows(rows)

    def _trades_from_rows(self, rows: list[sqlite3.Row]) -> list[PaperTrade]:
        trades: list[PaperTrade] = []
        for row in rows:
            try:
                trades.append(self._trade_from_row(row))
            except Exception:
                LOGGER.exception("skipping unreadable paper trade %s", row["trade_id"])
        return trades

    def archive_identities(self, session_id: str) -> None:
        """Free unique opportunity_id/trade_id for a fresh demo session. History remains."""

        suffix = f":archived:{session_id}"
        for trade in self.list_all():
            if ":archived:" in trade.trade_id:
                continue
            old_id = trade.trade_id
            new_id = f"{old_id}{suffix}"
            new_opp = f"{trade.opportunity_id}{suffix}"
            self._connection.execute(
                "UPDATE paper_trade_legs SET trade_id = ? WHERE trade_id = ?",
                (new_id, old_id),
            )
            self._connection.execute(
                "UPDATE paper_trade_events SET trade_id = ? WHERE trade_id = ?",
                (new_id, old_id),
            )
            self._connection.execute(
                "UPDATE paper_trades SET trade_id = ?, opportunity_id = ? WHERE trade_id = ?",
                (new_id, new_opp, old_id),
            )
        self._ledger._commit()

    def save(self, trade: PaperTrade) -> PaperTrade:
        existing = self.get(trade.trade_id)
        if existing is not None and existing.entry_risk is not None:
            trade.entry_risk = existing.entry_risk
            if existing.close_risks:
                known = {
                    (item.kind, item.recorded_at.isoformat(), item.score)
                    for item in trade.close_risks
                }
                for item in existing.close_risks:
                    key = (item.kind, item.recorded_at.isoformat(), item.score)
                    if key not in known:
                        trade.close_risks.append(item)
            if existing.close_fills and not trade.close_fills:
                trade.close_fills = list(existing.close_fills)
        payload = (
            trade.trade_id,
            trade.opportunity_id,
            trade.canonical_event_id,
            trade.canonical_market_id,
            trade.settlement_key,
            trade.solver_model,
            trade.market_family.value if trade.market_family else None,
            trade.period.value if trade.period else None,
            trade.competition,
            trade.home_team,
            trade.away_team,
            trade.fixture_label,
            trade.market_label,
            trade.state.value,
            trade.opened_at.isoformat(),
            trade.last_updated_at.isoformat(),
            trade.settled_at.isoformat() if trade.settled_at else None,
            _dec(trade.guaranteed_profit_gbp_at_open),
            _dec(trade.realised_pnl_gbp),
            json.dumps({key: str(value) for key, value in trade.capital_locked_native.items()}),
            _dec(trade.capital_locked_gbp),
            trade.settlement_outcome,
            trade.settlement_source,
            trade.settlement_source_id,
            trade.settlement_detail,
            trade.provenance.value,
            json.dumps([item.model_dump(mode="json") for item in trade.fx_snapshots]),
            json.dumps([item.model_dump(mode="json") for item in trade.venue_costs]),
            json.dumps(trade.entry_risk.model_dump(mode="json") if trade.entry_risk else None),
            json.dumps([item.model_dump(mode="json") for item in trade.close_risks]),
            json.dumps([item.model_dump(mode="json") for item in trade.close_fills]),
        )
        self._connection.execute(
            """
            INSERT INTO paper_trades (
                trade_id, opportunity_id, canonical_event_id, canonical_market_id,
                settlement_key, solver_model, market_family, period, competition, home_team, away_team,
                fixture_label, market_label, state, opened_at, last_updated_at, settled_at,
                guaranteed_profit_gbp_at_open, realised_pnl_gbp, capital_locked_native_json,
                capital_locked_gbp, settlement_outcome, settlement_source, settlement_source_id,
                settlement_detail, provenance, fx_snapshots_json, venue_costs_json,
                entry_risk_json, close_risks_json, close_fills_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO UPDATE SET
                opportunity_id = excluded.opportunity_id,
                canonical_event_id = excluded.canonical_event_id,
                canonical_market_id = excluded.canonical_market_id,
                settlement_key = excluded.settlement_key,
                solver_model = excluded.solver_model,
                market_family = excluded.market_family,
                period = excluded.period,
                competition = excluded.competition,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                fixture_label = excluded.fixture_label,
                market_label = excluded.market_label,
                state = excluded.state,
                opened_at = excluded.opened_at,
                last_updated_at = excluded.last_updated_at,
                settled_at = excluded.settled_at,
                guaranteed_profit_gbp_at_open = excluded.guaranteed_profit_gbp_at_open,
                realised_pnl_gbp = excluded.realised_pnl_gbp,
                capital_locked_native_json = excluded.capital_locked_native_json,
                capital_locked_gbp = excluded.capital_locked_gbp,
                settlement_outcome = excluded.settlement_outcome,
                settlement_source = excluded.settlement_source,
                settlement_source_id = excluded.settlement_source_id,
                settlement_detail = excluded.settlement_detail,
                provenance = excluded.provenance,
                fx_snapshots_json = excluded.fx_snapshots_json,
                venue_costs_json = excluded.venue_costs_json,
                entry_risk_json = COALESCE(paper_trades.entry_risk_json, excluded.entry_risk_json),
                close_risks_json = excluded.close_risks_json,
                close_fills_json = excluded.close_fills_json
            """,
            payload,
        )
        self._connection.execute("DELETE FROM paper_trade_legs WHERE trade_id = ?", (trade.trade_id,))
        for leg in trade.legs:
            self._connection.execute(
                """
                INSERT INTO paper_trade_legs (
                    trade_id, venue, outcome, currency, requested_stake, filled_stake,
                    displayed_odds, filled_odds, source_market_id, source_event_id,
                    source_runner_id, source_contract_id, opening_action, canonical_state,
                    settlement_fingerprint_key, fill_id, fill_kind, capital_source, execution_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.trade_id,
                    leg.venue.value,
                    leg.outcome,
                    leg.currency,
                    str(leg.requested_stake),
                    str(leg.filled_stake),
                    _dec(leg.displayed_odds),
                    _dec(leg.filled_odds),
                    leg.source_market_id,
                    leg.source_event_id,
                    leg.source_runner_id,
                    leg.source_contract_id,
                    None if leg.opening_action is None else leg.opening_action.value,
                    leg.canonical_state,
                    leg.settlement_fingerprint_key,
                    leg.fill_id,
                    leg.fill_kind.value,
                    leg.capital_source.value,
                    leg.execution_mode,
                ),
            )
        existing_events = {
            row["event_id"]
            for row in self._connection.execute(
                "SELECT event_id FROM paper_trade_events WHERE trade_id = ?",
                (trade.trade_id,),
            )
        }
        for event in trade.audit:
            if event.event_id in existing_events:
                continue
            self._connection.execute(
                """
                INSERT INTO paper_trade_events (event_id, trade_id, occurred_at, event_type, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    trade.trade_id,
                    event.occurred_at.isoformat(),
                    event.event_type.value,
                    event.detail,
                ),
            )
        self._ledger._commit()
        return trade

    def _trade_from_row(self, row: sqlite3.Row) -> PaperTrade:
        legs = []
        for item in self._connection.execute(
            "SELECT * FROM paper_trade_legs WHERE trade_id = ? ORDER BY rowid",
            (row["trade_id"],),
        ):
            try:
                legs.append(_leg_from_row(item))
            except Exception:
                LOGGER.exception(
                    "skipping unreadable paper trade leg for %s", row["trade_id"]
                )
        audit = []
        for item in self._connection.execute(
            "SELECT * FROM paper_trade_events WHERE trade_id = ? ORDER BY rowid",
            (row["trade_id"],),
        ):
            try:
                audit.append(
                    PaperTradeAuditEvent(
                        event_id=item["event_id"],
                        occurred_at=datetime.fromisoformat(item["occurred_at"]),
                        event_type=item["event_type"],
                        detail=item["detail"],
                    )
                )
            except Exception:
                LOGGER.exception(
                    "skipping unreadable paper trade audit event %s", item["event_id"]
                )
        native = {
            key: Decimal(value)
            for key, value in json.loads(row["capital_locked_native_json"] or "{}").items()
        }
        return PaperTrade(
            trade_id=row["trade_id"],
            opportunity_id=row["opportunity_id"],
            canonical_event_id=_row_value(row, "canonical_event_id"),
            canonical_market_id=_row_value(row, "canonical_market_id"),
            settlement_key=_row_value(row, "settlement_key"),
            solver_model=_row_value(row, "solver_model"),
            market_family=MarketFamily(row["market_family"]) if row["market_family"] else None,
            period=FootballPeriod(row["period"]) if row["period"] else None,
            competition=_row_value(row, "competition"),
            home_team=_row_value(row, "home_team"),
            away_team=_row_value(row, "away_team"),
            fixture_label=_row_value(row, "fixture_label"),
            market_label=_row_value(row, "market_label"),
            state=PaperTradeState(row["state"]),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            last_updated_at=datetime.fromisoformat(row["last_updated_at"]),
            settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
            guaranteed_profit_gbp_at_open=_decimal(row["guaranteed_profit_gbp_at_open"]),
            realised_pnl_gbp=_decimal(row["realised_pnl_gbp"]),
            capital_locked_native=native,
            capital_locked_gbp=_decimal(row["capital_locked_gbp"]),
            settlement_outcome=_row_value(row, "settlement_outcome"),
            settlement_source=_row_value(row, "settlement_source"),
            settlement_source_id=_row_value(row, "settlement_source_id"),
            settlement_detail=_row_value(row, "settlement_detail"),
            provenance=row["provenance"],
            legs=legs,
            fx_snapshots=_models_from_json(row["fx_snapshots_json"], FxRateSnapshot),
            venue_costs=_models_from_json(row["venue_costs_json"], VenueCostSnapshot),
            entry_risk=_entry_risk_from_row(row),
            close_risks=_close_risks_from_row(row),
            close_fills=_models_from_json(_row_value(row, "close_fills_json"), PaperCloseFill),
            audit=audit,
        )


class SqlitePaperLedger:
    """One SQLite file for paper trades, the paper subledger, and paper treasury."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        seed_gbp: Decimal = Decimal("1000"),
        usd_gbp_per_unit: Decimal = Decimal("0.80"),
        fx_source: str = "paper_demo_fx_snapshot",
        include_kalshi: bool = True,
        auto_seed: bool = True,
    ) -> None:
        from sports_hedge.treasury.service import PaperTreasuryService

        self._tx_depth = 0
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(database),
            check_same_thread=False,
            timeout=30.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.isolation_level = None
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        self.journal = SqlitePaperJournal(self)
        self.trades = SqlitePaperTradeRepository(self)
        self.treasury = PaperTreasuryService(self)
        if auto_seed:
            self.treasury.ensure_demo_session(
                seed_gbp=seed_gbp,
                usd_gbp_per_unit=usd_gbp_per_unit,
                fx_source=fx_source,
                include_kalshi=include_kalshi,
            )

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._lock:
            yield

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._tx_depth += 1
            started = self._tx_depth == 1
            if started:
                self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                if started:
                    self._connection.commit()
            except Exception:
                if started:
                    self._connection.rollback()
                    self.reload_journal()
                raise
            finally:
                self._tx_depth -= 1

    def _commit(self) -> None:
        if self._tx_depth == 0:
            self._connection.commit()

    def reload_journal(self) -> None:
        with self.exclusive():
            self.journal._memory = PaperJournal()
            self.journal._hydrate_memory()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_journal_entries (
                journal_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                opportunity_id TEXT NOT NULL,
                trade_id TEXT,
                occurred_at TEXT NOT NULL,
                provenance TEXT NOT NULL,
                entry_json TEXT NOT NULL,
                UNIQUE(source, source_id)
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL UNIQUE,
                canonical_event_id TEXT,
                canonical_market_id TEXT,
                settlement_key TEXT,
                solver_model TEXT,
                market_family TEXT,
                period TEXT,
                competition TEXT,
                home_team TEXT,
                away_team TEXT,
                fixture_label TEXT,
                market_label TEXT,
                state TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                last_updated_at TEXT NOT NULL,
                settled_at TEXT,
                guaranteed_profit_gbp_at_open TEXT,
                realised_pnl_gbp TEXT,
                capital_locked_native_json TEXT NOT NULL,
                capital_locked_gbp TEXT,
                settlement_outcome TEXT,
                settlement_source TEXT,
                settlement_source_id TEXT,
                settlement_detail TEXT,
                provenance TEXT NOT NULL,
                fx_snapshots_json TEXT NOT NULL,
                venue_costs_json TEXT NOT NULL,
                entry_risk_json TEXT,
                close_risks_json TEXT NOT NULL DEFAULT '[]',
                close_fills_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS paper_trade_legs (
                trade_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                outcome TEXT NOT NULL,
                currency TEXT NOT NULL,
                requested_stake TEXT NOT NULL,
                filled_stake TEXT NOT NULL,
                displayed_odds TEXT,
                filled_odds TEXT,
                source_market_id TEXT NOT NULL,
                source_event_id TEXT,
                source_runner_id TEXT,
                source_contract_id TEXT,
                opening_action TEXT,
                canonical_state TEXT,
                settlement_fingerprint_key TEXT,
                fill_id TEXT,
                fill_kind TEXT NOT NULL,
                capital_source TEXT NOT NULL,
                execution_mode TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_trade_events (
                event_id TEXT PRIMARY KEY,
                trade_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_paper_trades_state ON paper_trades(state);
            CREATE INDEX IF NOT EXISTS idx_paper_journal_opportunity
                ON paper_journal_entries(opportunity_id);

            CREATE TABLE IF NOT EXISTS paper_treasury_sessions (
                session_id TEXT PRIMARY KEY,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                active INTEGER NOT NULL,
                provenance TEXT NOT NULL,
                reason TEXT NOT NULL,
                seed_gbp TEXT NOT NULL,
                fx_rate_usd_gbp TEXT NOT NULL,
                fx_source TEXT NOT NULL,
                fx_as_of TEXT NOT NULL,
                include_kalshi INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_treasury_pools (
                pool_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                native_currency TEXT NOT NULL,
                seed_native TEXT NOT NULL,
                available_cash TEXT NOT NULL,
                locked_capital TEXT NOT NULL,
                realised_pnl_native TEXT NOT NULL,
                cumulative_fees_native TEXT NOT NULL,
                fx_rate_gbp_per_unit TEXT,
                fx_source TEXT,
                fx_as_of TEXT,
                UNIQUE(session_id, venue, native_currency)
            );

            CREATE TABLE IF NOT EXISTS paper_treasury_events (
                event_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                pool_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                native_currency TEXT NOT NULL,
                event_type TEXT NOT NULL,
                native_amount TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                trade_id TEXT,
                opportunity_id TEXT,
                lock_id TEXT,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                fx_rate_gbp_per_unit TEXT,
                fx_source TEXT,
                journal_id TEXT,
                UNIQUE(source, source_id)
            );

            CREATE TABLE IF NOT EXISTS paper_treasury_locks (
                lock_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                pool_id TEXT NOT NULL,
                trade_id TEXT,
                opportunity_id TEXT,
                venue TEXT NOT NULL,
                native_currency TEXT NOT NULL,
                locked_native TEXT NOT NULL,
                released_native TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'paper_fill_simulator',
                capital_source TEXT NOT NULL DEFAULT 'AUTO_POOL',
                fill_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_paper_treasury_events_session
                ON paper_treasury_events(session_id, occurred_at);
            """
        )
        self._connection.commit()
        self._ensure_unwind_identity_columns()
        self._ensure_treasury_lock_fact_columns()
        self._ensure_risk_snapshot_columns()
        self._ensure_close_fills_column()
        self._ensure_trade_leg_compat_columns()

    def _ensure_risk_snapshot_columns(self) -> None:
        trade_cols = {row[1] for row in self._connection.execute("PRAGMA table_info(paper_trades)")}
        if "entry_risk_json" not in trade_cols:
            self._connection.execute("ALTER TABLE paper_trades ADD COLUMN entry_risk_json TEXT")
        if "close_risks_json" not in trade_cols:
            self._connection.execute(
                "ALTER TABLE paper_trades ADD COLUMN close_risks_json TEXT NOT NULL DEFAULT '[]'"
            )
        self._connection.commit()

    def _ensure_close_fills_column(self) -> None:
        trade_cols = {row[1] for row in self._connection.execute("PRAGMA table_info(paper_trades)")}
        if "close_fills_json" not in trade_cols:
            self._connection.execute(
                "ALTER TABLE paper_trades ADD COLUMN close_fills_json TEXT NOT NULL DEFAULT '[]'"
            )
        self._connection.commit()

    def _ensure_unwind_identity_columns(self) -> None:
        trade_cols = {row[1] for row in self._connection.execute("PRAGMA table_info(paper_trades)")}
        if "solver_model" not in trade_cols:
            self._connection.execute("ALTER TABLE paper_trades ADD COLUMN solver_model TEXT")
        leg_cols = {row[1] for row in self._connection.execute("PRAGMA table_info(paper_trade_legs)")}
        additions = {
            "source_event_id": "TEXT",
            "source_runner_id": "TEXT",
            "source_contract_id": "TEXT",
            "opening_action": "TEXT",
            "canonical_state": "TEXT",
            "settlement_fingerprint_key": "TEXT",
        }
        for name, spec in additions.items():
            if name not in leg_cols:
                self._connection.execute(f"ALTER TABLE paper_trade_legs ADD COLUMN {name} {spec}")
        self._connection.commit()

    def _ensure_treasury_lock_fact_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(paper_treasury_locks)")
        }
        if "source" not in columns:
            self._connection.execute(
                "ALTER TABLE paper_treasury_locks ADD COLUMN source TEXT NOT NULL DEFAULT 'paper_fill_simulator'"
            )
        if "capital_source" not in columns:
            self._connection.execute(
                "ALTER TABLE paper_treasury_locks ADD COLUMN capital_source TEXT NOT NULL DEFAULT 'AUTO_POOL'"
            )
        if "fill_id" not in columns:
            self._connection.execute("ALTER TABLE paper_treasury_locks ADD COLUMN fill_id TEXT")
        self._connection.commit()

    def _ensure_trade_leg_compat_columns(self) -> None:
        """Historical paper_trade_legs rows predate several identity/risk columns."""

        if "paper_trade_legs" not in _table_names(self._connection):
            return
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(paper_trade_legs)")
        }
        additions = {
            "fill_id": "TEXT",
            "fill_kind": "TEXT NOT NULL DEFAULT 'INTERNAL_SIMULATED'",
            "capital_source": "TEXT NOT NULL DEFAULT 'AUTO_POOL'",
            "execution_mode": "TEXT NOT NULL DEFAULT 'INTERNAL'",
            "source_event_id": "TEXT",
            "source_runner_id": "TEXT",
            "source_contract_id": "TEXT",
            "opening_action": "TEXT",
            "canonical_state": "TEXT",
            "settlement_fingerprint_key": "TEXT",
        }
        for name, spec in additions.items():
            if name not in columns:
                self._connection.execute(f"ALTER TABLE paper_trade_legs ADD COLUMN {name} {spec}")
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        if key not in row.keys():
            return default
        value = row[key]
    except Exception:
        return default
    return default if value is None else value


def _optional_odds(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        odds = Decimal(str(value))
    except Exception:
        return None
    return odds if odds > 1 else None


def _leg_from_row(item: sqlite3.Row) -> PaperTradeLeg:
    return PaperTradeLeg.model_validate(
        {
            "venue": item["venue"],
            "outcome": item["outcome"],
            "currency": item["currency"],
            "requested_stake": item["requested_stake"],
            "filled_stake": item["filled_stake"],
            "displayed_odds": _optional_odds(_row_value(item, "displayed_odds")),
            "filled_odds": _optional_odds(_row_value(item, "filled_odds")),
            "source_market_id": _row_value(item, "source_market_id") or "unknown",
            "source_event_id": _row_value(item, "source_event_id"),
            "source_runner_id": _row_value(item, "source_runner_id"),
            "source_contract_id": _row_value(item, "source_contract_id"),
            "opening_action": _row_value(item, "opening_action"),
            "canonical_state": _row_value(item, "canonical_state"),
            "settlement_fingerprint_key": _row_value(item, "settlement_fingerprint_key"),
            "fill_id": _row_value(item, "fill_id"),
            "fill_kind": _row_value(item, "fill_kind") or "INTERNAL_SIMULATED",
            "capital_source": _row_value(item, "capital_source") or "AUTO_POOL",
            "execution_mode": _row_value(item, "execution_mode") or "INTERNAL",
        }
    )


def _aware_payload(payload: dict[str, Any]) -> dict[str, Any]:
    patched = dict(payload)
    for key in ("captured_at", "effective_from"):
        value = patched.get(key)
        if not isinstance(value, str) or not value:
            continue
        if value.endswith("Z") or value.endswith("z") or "+" in value[10:] or value[-6] == "-":
            continue
        patched[key] = f"{value}+00:00"
    return patched


def _models_from_json(raw: str | None, model: type) -> list[Any]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    loaded: list[Any] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            loaded.append(model.model_validate(item))
        except Exception:
            try:
                loaded.append(model.model_validate(_aware_payload(item)))
            except Exception:
                LOGGER.debug("skipping unreadable %s payload", model.__name__, exc_info=True)
    return loaded


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _entry_risk_from_row(row: sqlite3.Row) -> PaperExecutionRiskSnapshot | None:
    if "entry_risk_json" not in row.keys():
        return None
    raw = row["entry_risk_json"]
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if not payload:
            return None
        return PaperExecutionRiskSnapshot.model_validate(payload)
    except Exception:
        LOGGER.exception("skipping unreadable entry_risk_json for trade %s", row["trade_id"])
        return None


def _close_risks_from_row(row: sqlite3.Row) -> list[PaperExecutionRiskSnapshot]:
    if "close_risks_json" not in row.keys():
        return []
    raw = row["close_risks_json"]
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        LOGGER.exception("skipping unreadable close_risks_json for trade %s", row["trade_id"])
        return []
    if not isinstance(payload, list):
        return []
    loaded: list[PaperExecutionRiskSnapshot] = []
    for item in payload:
        try:
            loaded.append(PaperExecutionRiskSnapshot.model_validate(item))
        except Exception:
            LOGGER.exception("skipping unreadable close risk snapshot")
    return loaded
