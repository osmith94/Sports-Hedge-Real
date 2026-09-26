"""UI read models for the Operations Console.

The scanner keeps authoritative fixture and economics state. These models are
the projection sent to the console. HOT, BACKGROUND, and ACTIVE TRADE pricing
must not publish the universe catalogue. Scan-cycle history and deferred
fixture rows stay off the heartbeat.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

AWAITING_CROSS_VENUE_STATES = frozenset(
    {
        "single_venue_no_cross_venue_candidate",
        "cross_venue_unavailable",
        "hot_relationship_missing",
    }
)

CATALOGUE_FORBIDDEN_FIELDS = frozenset(
    {
        "matched_market_count",
        "discovered_market_count",
        "matched_equivalent_count",
        "qualifying_market_count",
        "near_executable_market_count",
        "best_arb_market",
        "headline_band",
        "best_matchbook_price",
        "best_polymarket_price",
        "best_kalshi_price",
        "current_net_edge",
        "trigger_net_edge",
        "distance_to_trigger_pp",
        "quote_age_ms",
        "quote_age_basis",
        "execution_risk_score",
        "execution_risk_band",
        "execution_risk_reasons",
        "solver_is_arbitrage",
        "opportunity_state",
        "market_evaluation_state",
        "market_evaluation_reason",
        "viability_evidence",
        "catalogue_coverage",
        "hot_reasons",
        "no_comparison_reason",
    }
)

HEARTBEAT_OMITTED_FIELDS = frozenset(
    {
        "discovered_fixtures",
        "recent_scan_cycles",
        "fixture_board_as_of",
    }
)


class UniverseCatalogueFixture(BaseModel):
    """One event the operator can recognise. No prices or arb economics."""

    canonical_event_id: str
    sport: str = "unknown"
    competition: str
    target_competition_code: str | None = None
    home_team: str
    away_team: str
    kickoff_utc: datetime
    matchbook_matched: bool = False
    polymarket_matched: bool = False
    kalshi_matched: bool = False
    fixture_status: str | None = None
    universe_generation_id: int | None = None
    updated_at: datetime | None = None


class UniverseCatalogueSnapshot(BaseModel):
    """Committed UNIVERSE catalogue. Static between UNIVERSE-facing changes."""

    data_kind: str = "universe_fixture_catalogue"
    paper_only: bool = True
    places_orders: bool = False
    fixtures: list[UniverseCatalogueFixture] = Field(default_factory=list)
    universe_fixture_count: int = 0
    universe_catalogue_version: str
    universe_catalogue_as_of: datetime | None = None
    universe_generation_id: int | None = None


class HotRosterEntry(BaseModel):
    """Compact HOT row. Deferred fixtures are not included."""

    canonical_event_id: str
    home_team: str
    away_team: str
    competition: str
    sport: str = "unknown"
    target_competition_code: str | None = None
    kickoff_utc: datetime
    in_running: bool | None = None
    fixture_status: str | None = None
    live_score_supported: bool = False
    home_score: int | None = None
    away_score: int | None = None
    hot_reasons: list[str] = Field(default_factory=list)
    scan_lane: str = "hot"
    market_evaluation_state: str | None = None
    market_evaluation_reason: str | None = None
    solver_is_arbitrage: bool = False
    matchbook_matched: bool = False
    polymarket_matched: bool = False
    kalshi_matched: bool = False
    last_scanned_at: datetime | None = None
    last_seen_at: datetime | None = None
    matched_equivalent_count: int | None = None
    current_net_edge: Decimal | None = None


class DeferredFixtureRow(BaseModel):
    """On-demand deferred row. Not part of the heartbeat."""

    canonical_event_id: str
    home_team: str
    away_team: str
    competition: str
    sport: str = "unknown"
    target_competition_code: str | None = None
    kickoff_utc: datetime
    fixture_status: str | None = None
    in_running: bool | None = None
    matchbook_matched: bool = False
    polymarket_matched: bool = False
    kalshi_matched: bool = False
    market_evaluation_state: str | None = None
    market_evaluation_reason: str | None = None
    hot_reasons: list[str] = Field(default_factory=list)
    scan_lane: str | None = None
    last_scanned_at: datetime | None = None
    last_seen_at: datetime | None = None


class DeferredFixtureReport(BaseModel):
    data_kind: str = "deferred_fixture_diagnostic"
    paper_only: bool = True
    places_orders: bool = False
    execution_enabled: bool = False
    provider_calls: int = 0
    scan_started: bool = False
    count: int = 0
    rows: list[DeferredFixtureRow] = Field(default_factory=list)
    note: str = (
        "Retained current-state evidence only. This read does not call a "
        "provider or start a scan."
    )


def catalogue_version(rows: list[UniverseCatalogueFixture]) -> str:
    """Stable revision of recognition fields. Touch time is not part of it."""

    payload = [
        row.model_dump(mode="json", exclude={"updated_at"})
        for row in sorted(rows, key=lambda item: item.canonical_event_id)
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def catalogue_recognition(row: UniverseCatalogueFixture) -> dict[str, Any]:
    return row.model_dump(mode="json", exclude={"updated_at"})


class UniverseCatalogueMemory:
    """Process-local catalogue. Mutations are explicit UNIVERSE-facing events."""

    def __init__(self) -> None:
        self.rows: dict[str, UniverseCatalogueFixture] = {}
        self.version = catalogue_version([])
        self.as_of: datetime | None = None
        self.generation_id: int | None = None

    def snapshot(self) -> UniverseCatalogueSnapshot:
        rows = sorted(self.rows.values(), key=lambda row: (row.kickoff_utc, row.canonical_event_id))
        return UniverseCatalogueSnapshot(
            fixtures=rows,
            universe_fixture_count=len(rows),
            universe_catalogue_version=self.version,
            universe_catalogue_as_of=self.as_of,
            universe_generation_id=self.generation_id,
        )

    def upsert(self, row: UniverseCatalogueFixture, *, seen_at: datetime) -> bool:
        previous = self.rows.get(row.canonical_event_id)
        if previous is not None and catalogue_recognition(previous) == catalogue_recognition(row):
            return False
        self.rows[row.canonical_event_id] = row
        self._commit(seen_at, row.universe_generation_id)
        return True

    def remove(self, canonical_event_id: str, *, seen_at: datetime) -> bool:
        if canonical_event_id not in self.rows:
            return False
        self.rows.pop(canonical_event_id, None)
        self._commit(seen_at, self.generation_id)
        return True

    def clear(self, *, seen_at: datetime | None) -> bool:
        if not self.rows and self.version == catalogue_version([]):
            return False
        self.rows.clear()
        self.generation_id = None
        self._commit(seen_at, None)
        return True

    def _commit(self, seen_at: datetime | None, generation_id: int | None) -> None:
        self.version = catalogue_version(list(self.rows.values()))
        self.as_of = seen_at
        if generation_id is not None or not self.rows:
            self.generation_id = generation_id
