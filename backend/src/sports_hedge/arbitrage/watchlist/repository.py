from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_hedge.arbitrage.watchlist.models import (
    LifecycleEventType,
    NearOpportunity,
    OpportunityClassification,
    OpportunityLifecycleEvent,
    OpportunityStatus,
    WatchLeg,
)
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName


class SqliteWatchlistRepository:
    """Current-opportunity snapshot plus append-only lifecycle events."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist_opportunities (
                opportunity_id TEXT PRIMARY KEY,
                canonical_event_id TEXT NOT NULL,
                canonical_market_id TEXT NOT NULL,
                settlement_key TEXT,
                competition TEXT,
                home_team TEXT,
                away_team TEXT,
                market_family TEXT,
                period TEXT,
                venues_json TEXT NOT NULL,
                legs_json TEXT NOT NULL,
                status TEXT NOT NULL,
                classification TEXT NOT NULL,
                is_arbitrage INTEGER NOT NULL,
                trigger_net_edge TEXT NOT NULL,
                current_net_edge TEXT,
                distance_to_trigger_pp TEXT,
                implied_probability_sum TEXT,
                quote_age_ms INTEGER NOT NULL,
                limiting_depth_gbp TEXT,
                limiting_leg_outcome TEXT,
                capital_required_gbp TEXT,
                guaranteed_profit_gbp TEXT,
                execution_risk_score INTEGER,
                expected_lock_minutes TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                rejection_reasons_json TEXT NOT NULL,
                insufficiency_reasons_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist_lifecycle_events (
                event_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                current_net_edge TEXT,
                distance_to_trigger_pp TEXT,
                detail TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_watchlist_status
                ON watchlist_opportunities(status, last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_watchlist_market
                ON watchlist_opportunities(canonical_market_id);
            CREATE INDEX IF NOT EXISTS idx_watchlist_events_time
                ON watchlist_lifecycle_events(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_watchlist_events_opportunity
                ON watchlist_lifecycle_events(opportunity_id, occurred_at);
            """
        )
        self._connection.commit()

    def get(self, opportunity_id: str) -> NearOpportunity | None:
        row = self._connection.execute(
            "SELECT * FROM watchlist_opportunities WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()
        return None if row is None else _opportunity_from_row(row)

    def upsert_opportunity(self, opportunity: NearOpportunity) -> None:
        self._connection.execute(
            """
            INSERT INTO watchlist_opportunities (
                opportunity_id, canonical_event_id, canonical_market_id, settlement_key,
                competition, home_team, away_team, market_family, period, venues_json,
                legs_json, status, classification, is_arbitrage, trigger_net_edge,
                current_net_edge, distance_to_trigger_pp, implied_probability_sum,
                quote_age_ms, limiting_depth_gbp, limiting_leg_outcome,
                capital_required_gbp, guaranteed_profit_gbp, execution_risk_score,
                expected_lock_minutes, first_seen_at, last_seen_at,
                rejection_reasons_json, insufficiency_reasons_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(opportunity_id) DO UPDATE SET
                canonical_event_id = excluded.canonical_event_id,
                canonical_market_id = excluded.canonical_market_id,
                settlement_key = excluded.settlement_key,
                competition = excluded.competition,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                market_family = excluded.market_family,
                period = excluded.period,
                venues_json = excluded.venues_json,
                legs_json = excluded.legs_json,
                status = excluded.status,
                classification = excluded.classification,
                is_arbitrage = excluded.is_arbitrage,
                trigger_net_edge = excluded.trigger_net_edge,
                current_net_edge = excluded.current_net_edge,
                distance_to_trigger_pp = excluded.distance_to_trigger_pp,
                implied_probability_sum = excluded.implied_probability_sum,
                quote_age_ms = excluded.quote_age_ms,
                limiting_depth_gbp = excluded.limiting_depth_gbp,
                limiting_leg_outcome = excluded.limiting_leg_outcome,
                capital_required_gbp = excluded.capital_required_gbp,
                guaranteed_profit_gbp = excluded.guaranteed_profit_gbp,
                execution_risk_score = excluded.execution_risk_score,
                expected_lock_minutes = excluded.expected_lock_minutes,
                first_seen_at = excluded.first_seen_at,
                last_seen_at = excluded.last_seen_at,
                rejection_reasons_json = excluded.rejection_reasons_json,
                insufficiency_reasons_json = excluded.insufficiency_reasons_json
            """,
            (
                opportunity.opportunity_id,
                opportunity.canonical_event_id,
                opportunity.canonical_market_id,
                opportunity.settlement_key,
                opportunity.competition,
                opportunity.home_team,
                opportunity.away_team,
                opportunity.market_family.value if opportunity.market_family else None,
                opportunity.period.value if opportunity.period else None,
                json.dumps([venue.value for venue in opportunity.venues]),
                json.dumps([leg.model_dump(mode="json") for leg in opportunity.legs]),
                opportunity.status.value,
                opportunity.classification.value,
                int(opportunity.is_arbitrage),
                _stringify(opportunity.trigger_net_edge),
                _stringify(opportunity.current_net_edge),
                _stringify(opportunity.distance_to_trigger_pp),
                _stringify(opportunity.implied_probability_sum),
                opportunity.quote_age_ms,
                _stringify(opportunity.limiting_depth_gbp),
                opportunity.limiting_leg_outcome,
                _stringify(opportunity.capital_required_gbp),
                _stringify(opportunity.guaranteed_profit_gbp),
                opportunity.execution_risk_score,
                _stringify(opportunity.expected_lock_minutes),
                opportunity.first_seen_at.isoformat(),
                opportunity.last_seen_at.isoformat(),
                json.dumps(opportunity.rejection_reasons),
                json.dumps(opportunity.insufficiency_reasons),
            ),
        )
        self._connection.commit()

    def append_event(self, event: OpportunityLifecycleEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO watchlist_lifecycle_events (
                event_id, opportunity_id, occurred_at, event_type, status,
                current_net_edge, distance_to_trigger_pp, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.opportunity_id,
                event.occurred_at.isoformat(),
                event.event_type.value,
                event.status.value,
                _stringify(event.current_net_edge),
                _stringify(event.distance_to_trigger_pp),
                event.detail,
            ),
        )
        self._connection.commit()

    def list_opportunities(self) -> list[NearOpportunity]:
        rows = self._connection.execute(
            "SELECT * FROM watchlist_opportunities ORDER BY last_seen_at DESC"
        ).fetchall()
        return [_opportunity_from_row(row) for row in rows]

    def list_events(
        self,
        *,
        limit: int = 100,
        opportunity_id: str | None = None,
        since: datetime | None = None,
    ) -> list[OpportunityLifecycleEvent]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[Any] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            parameters.append(opportunity_id)
        if since is not None:
            clauses.append("occurred_at >= ?")
            parameters.append(since.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM watchlist_lifecycle_events{where} "  # noqa: S608
            "ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
            parameters,
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def close(self) -> None:
        self._connection.close()


def _opportunity_from_row(row: sqlite3.Row) -> NearOpportunity:
    return NearOpportunity(
        opportunity_id=row["opportunity_id"],
        canonical_event_id=row["canonical_event_id"],
        canonical_market_id=row["canonical_market_id"],
        settlement_key=row["settlement_key"],
        competition=row["competition"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        market_family=MarketFamily(row["market_family"]) if row["market_family"] else None,
        period=FootballPeriod(row["period"]) if row["period"] else None,
        venues=[VenueName(value) for value in json.loads(row["venues_json"])],
        legs=[WatchLeg.model_validate(item) for item in json.loads(row["legs_json"])],
        status=OpportunityStatus(row["status"]),
        classification=OpportunityClassification(row["classification"]),
        is_arbitrage=bool(row["is_arbitrage"]),
        trigger_net_edge=Decimal(row["trigger_net_edge"]),
        current_net_edge=_decimal(row["current_net_edge"]),
        distance_to_trigger_pp=_decimal(row["distance_to_trigger_pp"]),
        implied_probability_sum=_decimal(row["implied_probability_sum"]),
        quote_age_ms=int(row["quote_age_ms"]),
        limiting_depth_gbp=_decimal(row["limiting_depth_gbp"]),
        limiting_leg_outcome=row["limiting_leg_outcome"],
        capital_required_gbp=_decimal(row["capital_required_gbp"]),
        guaranteed_profit_gbp=_decimal(row["guaranteed_profit_gbp"]),
        execution_risk_score=row["execution_risk_score"],
        expected_lock_minutes=_decimal(row["expected_lock_minutes"]),
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        rejection_reasons=list(json.loads(row["rejection_reasons_json"])),
        insufficiency_reasons=list(json.loads(row["insufficiency_reasons_json"])),
    )


def _event_from_row(row: sqlite3.Row) -> OpportunityLifecycleEvent:
    return OpportunityLifecycleEvent(
        event_id=row["event_id"],
        opportunity_id=row["opportunity_id"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        event_type=LifecycleEventType(row["event_type"]),
        status=OpportunityStatus(row["status"]),
        current_net_edge=_decimal(row["current_net_edge"]),
        distance_to_trigger_pp=_decimal(row["distance_to_trigger_pp"]),
        detail=row["detail"],
    )


def _stringify(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
