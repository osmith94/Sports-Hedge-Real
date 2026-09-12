"""SQLite paper trade book and append-only paper subledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from sqlite3 import IntegrityError

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
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeLeg,
    PaperTradeState,
)


class SqlitePaperJournal:
    """Durable wrap of the in-memory PaperJournal contract."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
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
            self._connection.commit()
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


class SqlitePaperTradeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

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
        return [self._trade_from_row(row) for row in rows]

    def list_closed(self) -> list[PaperTrade]:
        rows = self._connection.execute(
            "SELECT * FROM paper_trades WHERE state = ? ORDER BY settled_at DESC, opened_at DESC",
            (PaperTradeState.CLOSED.value,),
        ).fetchall()
        return [self._trade_from_row(row) for row in rows]

    def save(self, trade: PaperTrade) -> PaperTrade:
        payload = (
            trade.trade_id,
            trade.opportunity_id,
            trade.canonical_event_id,
            trade.canonical_market_id,
            trade.settlement_key,
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
        )
        self._connection.execute(
            """
            INSERT INTO paper_trades (
                trade_id, opportunity_id, canonical_event_id, canonical_market_id,
                settlement_key, market_family, period, competition, home_team, away_team,
                fixture_label, market_label, state, opened_at, last_updated_at, settled_at,
                guaranteed_profit_gbp_at_open, realised_pnl_gbp, capital_locked_native_json,
                capital_locked_gbp, settlement_outcome, settlement_source, settlement_source_id,
                settlement_detail, provenance, fx_snapshots_json, venue_costs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO UPDATE SET
                opportunity_id = excluded.opportunity_id,
                canonical_event_id = excluded.canonical_event_id,
                canonical_market_id = excluded.canonical_market_id,
                settlement_key = excluded.settlement_key,
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
                venue_costs_json = excluded.venue_costs_json
            """,
            payload,
        )
        self._connection.execute("DELETE FROM paper_trade_legs WHERE trade_id = ?", (trade.trade_id,))
        for leg in trade.legs:
            self._connection.execute(
                """
                INSERT INTO paper_trade_legs (
                    trade_id, venue, outcome, currency, requested_stake, filled_stake,
                    displayed_odds, filled_odds, source_market_id, fill_id, fill_kind,
                    capital_source, execution_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self._connection.commit()
        return trade

    def _trade_from_row(self, row: sqlite3.Row) -> PaperTrade:
        legs = [
            PaperTradeLeg.model_validate(
                {
                    "venue": item["venue"],
                    "outcome": item["outcome"],
                    "currency": item["currency"],
                    "requested_stake": item["requested_stake"],
                    "filled_stake": item["filled_stake"],
                    "displayed_odds": item["displayed_odds"],
                    "filled_odds": item["filled_odds"],
                    "source_market_id": item["source_market_id"],
                    "fill_id": item["fill_id"],
                    "fill_kind": item["fill_kind"],
                    "capital_source": item["capital_source"],
                    "execution_mode": item["execution_mode"],
                }
            )
            for item in self._connection.execute(
                "SELECT * FROM paper_trade_legs WHERE trade_id = ? ORDER BY rowid",
                (row["trade_id"],),
            )
        ]
        audit = [
            PaperTradeAuditEvent(
                event_id=item["event_id"],
                occurred_at=datetime.fromisoformat(item["occurred_at"]),
                event_type=item["event_type"],
                detail=item["detail"],
            )
            for item in self._connection.execute(
                "SELECT * FROM paper_trade_events WHERE trade_id = ? ORDER BY rowid",
                (row["trade_id"],),
            )
        ]
        native = {
            key: Decimal(value)
            for key, value in json.loads(row["capital_locked_native_json"] or "{}").items()
        }
        return PaperTrade(
            trade_id=row["trade_id"],
            opportunity_id=row["opportunity_id"],
            canonical_event_id=row["canonical_event_id"],
            canonical_market_id=row["canonical_market_id"],
            settlement_key=row["settlement_key"],
            market_family=MarketFamily(row["market_family"]) if row["market_family"] else None,
            period=FootballPeriod(row["period"]) if row["period"] else None,
            competition=row["competition"],
            home_team=row["home_team"],
            away_team=row["away_team"],
            fixture_label=row["fixture_label"],
            market_label=row["market_label"],
            state=PaperTradeState(row["state"]),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            last_updated_at=datetime.fromisoformat(row["last_updated_at"]),
            settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
            guaranteed_profit_gbp_at_open=_decimal(row["guaranteed_profit_gbp_at_open"]),
            realised_pnl_gbp=_decimal(row["realised_pnl_gbp"]),
            capital_locked_native=native,
            capital_locked_gbp=_decimal(row["capital_locked_gbp"]),
            settlement_outcome=row["settlement_outcome"],
            settlement_source=row["settlement_source"],
            settlement_source_id=row["settlement_source_id"],
            settlement_detail=row["settlement_detail"],
            provenance=row["provenance"],
            legs=legs,
            fx_snapshots=[
                FxRateSnapshot.model_validate(item)
                for item in json.loads(row["fx_snapshots_json"] or "[]")
            ],
            venue_costs=[
                VenueCostSnapshot.model_validate(item)
                for item in json.loads(row["venue_costs_json"] or "[]")
            ],
            audit=audit,
        )


class SqlitePaperLedger:
    """One SQLite file for paper trades and the paper subledger."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()
        self.journal = SqlitePaperJournal(self._connection)
        self.trades = SqlitePaperTradeRepository(self._connection)

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
                venue_costs_json TEXT NOT NULL
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
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
