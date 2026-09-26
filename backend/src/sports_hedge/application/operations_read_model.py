"""UI read models for the Operations Console.

The scanner keeps authoritative fixture and economics state. These models are
the projection sent to the console. HOT, BACKGROUND, and ACTIVE TRADE pricing
must not publish the universe catalogue. Scan-cycle history and deferred
fixture rows stay off the heartbeat.
"""

from __future__ import annotations

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

# Lane diagnostics the console does not render. They stay on the stored lane
# status and on the on-demand cycle report. The 2-second poll does not ship them.
HEARTBEAT_BULK_DIAGNOSTIC_KEYS = frozenset(
    {
        "series_results",
        "polymarket_series_results",
        "identity_shards",
        "identity_scope",
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


def strip_heartbeat_bulk_diagnostics(payload: dict[str, Any]) -> None:
    """Drop diagnostic lists from a heartbeat dict. Does not mutate stored status."""

    for lane_name in ("hot", "universe", "background", "active_trade"):
        lane = payload.get(lane_name)
        if not isinstance(lane, dict):
            continue
        diagnostics = lane.get("last_diagnostics")
        if not isinstance(diagnostics, dict):
            continue
        lane["last_diagnostics"] = {
            key: value
            for key, value in diagnostics.items()
            if key not in HEARTBEAT_BULK_DIAGNOSTIC_KEYS
        }


def catalogue_recognition(row: UniverseCatalogueFixture) -> dict[str, Any]:
    return row.model_dump(mode="json", exclude={"updated_at"})


class UniverseCatalogueMetadata(BaseModel):
    """Heartbeat fields. Counting and reading the revision does not sort fixtures."""

    universe_fixture_count: int = 0
    universe_catalogue_version: str = "0"
    universe_catalogue_as_of: datetime | None = None
    universe_generation_id: int | None = None


class UniverseCatalogueMemory:
    """Process-local catalogue. Mutations are explicit UNIVERSE-facing events.

    ``revision`` is a monotonic token. It advances by one when recognition
    content changes. A pure touch does not advance it, and the heartbeat can
    read it without sorting or hashing the catalogue.
    """

    def __init__(self) -> None:
        self.rows: dict[str, UniverseCatalogueFixture] = {}
        self.revision = 0
        self.version = "0"
        self.as_of: datetime | None = None
        self.generation_id: int | None = None

    def metadata(self) -> UniverseCatalogueMetadata:
        return UniverseCatalogueMetadata(
            universe_fixture_count=len(self.rows),
            universe_catalogue_version=self.version,
            universe_catalogue_as_of=self.as_of,
            universe_generation_id=self.generation_id,
        )

    def snapshot(self) -> UniverseCatalogueSnapshot:
        rows = sorted(self.rows.values(), key=lambda row: (row.kickoff_utc, row.canonical_event_id))
        meta = self.metadata()
        return UniverseCatalogueSnapshot(
            fixtures=rows,
            universe_fixture_count=meta.universe_fixture_count,
            universe_catalogue_version=meta.universe_catalogue_version,
            universe_catalogue_as_of=meta.universe_catalogue_as_of,
            universe_generation_id=meta.universe_generation_id,
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
        if not self.rows:
            return False
        self.rows.clear()
        self.generation_id = None
        self._commit(seen_at, None)
        return True

    def _commit(self, seen_at: datetime | None, generation_id: int | None) -> None:
        self.revision += 1
        self.version = str(self.revision)
        self.as_of = seen_at
        if generation_id is not None or not self.rows:
            self.generation_id = generation_id
