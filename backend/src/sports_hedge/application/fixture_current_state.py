from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    FixtureDetailReadModel,
    FixtureMarketInventoryRow,
    MarketEvaluationState,
)
from sports_hedge.application.scan_lanes import (
    DEFAULT_HOT_HORIZON,
    DEFAULT_HOT_INTERVAL_SECONDS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
    DEFAULT_UNIVERSE_INTERVAL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    FRESHNESS_EXPIRED,
    ScanLane,
    classify_scan_lane,
    freshness_class,
    hot_sort_key,
    next_due_at,
)
from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.models import PaperScanDecision


@dataclass
class StoredSourceEvent:
    venue: VenueName
    source_event_id: str
    raw: dict[str, Any]


@dataclass
class LaneObservation:
    fixture: DiscoveredFixture
    markets: list[FixtureMarketInventoryRow]
    scan_lane: ScanLane
    last_scanned_at: datetime
    paper_market_ids: tuple[str, ...]
    source_events: tuple[StoredSourceEvent, ...] = ()
    evaluated: bool = True


@dataclass
class FixtureRadarRow:
    canonical_event_id: str
    fixture: DiscoveredFixture
    markets: list[FixtureMarketInventoryRow]
    membership: ScanLane
    observation_lane: ScanLane
    last_scanned_at: datetime
    paper_market_ids: tuple[str, ...]
    freshness: str
    next_due_at: datetime | None
    source_events: tuple[StoredSourceEvent, ...] = ()


class FixtureCurrentStateStore:
    """Process-memory canonical fixture current-state (v1).

    Coordinator-owned. Dual-cadence HOT/UNIVERSE lanes upsert into this same
    store with TTL merge. One identity map; no second canonical system.

    Do not fuzzy-match fixture names. Do not fabricate demo fixtures.
    """

    def __init__(self) -> None:
        self._generation = 0
        self._rows: dict[str, _FixtureRecord] = {}
        self._aliases: dict[str, str] = {}
        self._has_collection = False

    def clear(self) -> None:
        self._generation = 0
        self._rows = {}
        self._aliases = {}
        self._has_collection = False

    @property
    def generation(self) -> int:
        return self._generation

    def replace_from_report(self, report: CollectionReport) -> None:
        """Compatibility generation replace used by explicit diagnostic collects."""

        self.clear()
        self.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE)

    def upsert_from_report(
        self,
        report: CollectionReport,
        *,
        scan_lane: ScanLane | str = ScanLane.UNIVERSE,
        now: datetime | None = None,
    ) -> None:
        lane = ScanLane(scan_lane) if not isinstance(scan_lane, ScanLane) else scan_lane
        if lane is ScanLane.DROP:
            lane = ScanLane.UNIVERSE
        scanned_at = require_aware_instant(now or report.completed_at, "last_scanned_at")
        fixtures = {
            fixture.canonical_event_id: fixture for fixture in report.discovered_fixtures
        }
        markets = {
            event_id: list(rows) for event_id, rows in report.fixture_markets.items()
        }
        paper_ids = _paper_market_ids_by_fixture(report)
        source_events = _source_events_from_report(report, fixtures)
        incoming_aliases = _aliases_from_report(report, fixtures)
        for canonical_id, fixture in fixtures.items():
            membership = classify_scan_lane(fixture, scanned_at)
            if membership is ScanLane.DROP:
                self._drop_identity(canonical_id)
                continue
            evaluated = (
                fixture.market_evaluation_state == MarketEvaluationState.EVALUATED.value
            )
            observation = LaneObservation(
                fixture=_stamp_fixture(fixture, lane=lane, last_scanned_at=scanned_at),
                markets=list(markets.get(canonical_id, [])),
                scan_lane=lane,
                last_scanned_at=scanned_at,
                paper_market_ids=paper_ids.get(canonical_id, ()),
                source_events=source_events.get(canonical_id, ()),
                evaluated=evaluated,
            )
            self._upsert_observation(canonical_id, observation)
        for alias, canonical_id in incoming_aliases.items():
            if canonical_id in self._rows:
                self._aliases[alias] = canonical_id
        for alias, canonical_id in list(self._aliases.items()):
            if canonical_id not in self._rows:
                self._aliases.pop(alias, None)
        self._generation += 1
        self._has_collection = True

    def resolve_canonical_id(self, identity: str) -> str | None:
        wanted = identity.strip()
        if not wanted:
            return None
        canonical_id = self._aliases.get(wanted)
        if canonical_id is None and wanted in self._rows:
            return wanted
        if canonical_id is None or canonical_id not in self._rows:
            return None
        return canonical_id

    def identities_for(self, identity: str) -> frozenset[str]:
        canonical_id = self.resolve_canonical_id(identity)
        if canonical_id is None:
            return frozenset()
        aliases = {alias for alias, target in self._aliases.items() if target == canonical_id}
        aliases.add(canonical_id)
        return frozenset(aliases)

    def detail(self, identity: str) -> FixtureDetailReadModel | None:
        canonical_id = self.resolve_canonical_id(identity)
        if canonical_id is None:
            return None
        record = self._rows[canonical_id]
        displayed = record.display_fixture()
        if displayed is None:
            return None
        return FixtureDetailReadModel(
            fixture=displayed,
            markets=list(record.display_markets()),
        )

    def has_collection(self) -> bool:
        return self._has_collection

    def inventory(
        self,
        now: datetime,
        *,
        hot_horizon=DEFAULT_HOT_HORIZON,
        post_kickoff_unknown_horizon=DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
        hot_interval_seconds: int = DEFAULT_HOT_INTERVAL_SECONDS,
        universe_interval_seconds: int = DEFAULT_UNIVERSE_INTERVAL_SECONDS,
    ) -> list[DiscoveredFixture]:
        rows: list[DiscoveredFixture] = []
        for record in self._rows.values():
            fixture = record.display_fixture()
            if fixture is None:
                continue
            membership = classify_scan_lane(
                fixture,
                now,
                hot_horizon=hot_horizon,
                post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
            )
            if membership is ScanLane.DROP:
                continue
            observation = record.selected_observation(membership)
            lane = observation.scan_lane if observation is not None else membership
            scanned = observation.last_scanned_at if observation is not None else fixture.last_seen_at
            rows.append(
                fixture.model_copy(
                    update={
                        "scan_lane": lane.value,
                        "last_scanned_at": scanned,
                        "next_due_at": next_due_at(
                            scanned,
                            lane,
                            hot_interval_seconds=hot_interval_seconds,
                            universe_interval_seconds=universe_interval_seconds,
                        ),
                    }
                )
            )
        rows.sort(key=lambda item: (item.kickoff_utc, item.canonical_event_id))
        return rows

    def current_radar_rows(
        self,
        now: datetime,
        *,
        hot_horizon=DEFAULT_HOT_HORIZON,
        post_kickoff_unknown_horizon=DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
        hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
        hot_interval_seconds: int = DEFAULT_HOT_INTERVAL_SECONDS,
        universe_interval_seconds: int = DEFAULT_UNIVERSE_INTERVAL_SECONDS,
        quote_age_ms_by_market: dict[str, int | None] | None = None,
        max_quote_age_ms: int = 1000,
    ) -> list[FixtureRadarRow]:
        evaluated = require_aware_instant(now, "now")
        quote_ages = quote_age_ms_by_market or {}
        current: list[FixtureRadarRow] = []
        drop_ids: list[str] = []
        for canonical_id, record in self._rows.items():
            fixture = record.display_fixture()
            if fixture is None:
                continue
            membership = classify_scan_lane(
                fixture,
                evaluated,
                hot_horizon=hot_horizon,
                post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
            )
            if membership is ScanLane.DROP:
                drop_ids.append(canonical_id)
                continue
            observation = record.selected_observation(membership)
            if observation is None or not observation.evaluated:
                continue
            market_quote_age = None
            for market_id in observation.paper_market_ids:
                if market_id in quote_ages:
                    market_quote_age = quote_ages[market_id]
                    break
            freshness = freshness_class(
                lane=observation.scan_lane,
                last_scanned_at=observation.last_scanned_at,
                now=evaluated,
                quote_age_ms=market_quote_age,
                max_quote_age_ms=max_quote_age_ms,
                hot_ttl_seconds=hot_ttl_seconds,
                universe_ttl_seconds=universe_ttl_seconds,
            )
            if freshness == FRESHNESS_EXPIRED:
                continue
            current.append(
                FixtureRadarRow(
                    canonical_event_id=canonical_id,
                    fixture=observation.fixture,
                    markets=list(observation.markets),
                    membership=membership,
                    observation_lane=observation.scan_lane,
                    last_scanned_at=observation.last_scanned_at,
                    paper_market_ids=observation.paper_market_ids,
                    freshness=freshness,
                    next_due_at=next_due_at(
                        observation.last_scanned_at,
                        observation.scan_lane,
                        hot_interval_seconds=hot_interval_seconds,
                        universe_interval_seconds=universe_interval_seconds,
                    ),
                    source_events=observation.source_events,
                )
            )
        for canonical_id in drop_ids:
            self._drop_identity(canonical_id)
        return current

    def current_tracked_opportunity_ids(self, now: datetime, **kwargs: Any) -> set[str]:
        from sports_hedge.arbitrage.watchlist.ranking import opportunity_id_for_canonical_market

        ids: set[str] = set()
        for row in self.current_radar_rows(now, **kwargs):
            for market_id in row.paper_market_ids:
                ids.add(opportunity_id_for_canonical_market(market_id))
        return ids

    def radar_meta_for_market(
        self,
        canonical_market_id: str,
        now: datetime,
        **kwargs: Any,
    ) -> FixtureRadarRow | None:
        for row in self.current_radar_rows(now, **kwargs):
            if canonical_market_id in row.paper_market_ids:
                return row
        return None

    def hot_identity_scope(self, now: datetime, **kwargs: Any) -> list[str]:
        evaluated = require_aware_instant(now, "now")
        hot_horizon = kwargs.get("hot_horizon", DEFAULT_HOT_HORIZON)
        post = kwargs.get(
            "post_kickoff_unknown_horizon", DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON
        )
        fixtures: list[DiscoveredFixture] = []
        for record in self._rows.values():
            fixture = record.display_fixture()
            if fixture is None:
                continue
            membership = classify_scan_lane(
                fixture,
                evaluated,
                hot_horizon=hot_horizon,
                post_kickoff_unknown_horizon=post,
            )
            if membership is ScanLane.HOT:
                fixtures.append(fixture)
        fixtures.sort(key=hot_sort_key)
        return [item.canonical_event_id for item in fixtures]

    def known_source_events(self, canonical_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        payload: dict[str, list[dict[str, Any]]] = {}
        for canonical_id in canonical_ids:
            record = self._rows.get(canonical_id)
            if record is None:
                continue
            events = record.source_events()
            if not events:
                continue
            payload[canonical_id] = [
                {
                    "venue": item.venue.value,
                    "source_event_id": item.source_event_id,
                    "raw": item.raw,
                }
                for item in events
            ]
        return payload

    def membership_counts(self, now: datetime, **kwargs: Any) -> tuple[int, int]:
        hot = 0
        universe = 0
        for record in self._rows.values():
            fixture = record.display_fixture()
            if fixture is None:
                continue
            membership = classify_scan_lane(fixture, now, **kwargs)
            if membership is ScanLane.HOT:
                hot += 1
            elif membership is ScanLane.UNIVERSE:
                universe += 1
        return hot, universe

    def _upsert_observation(self, canonical_id: str, observation: LaneObservation) -> None:
        record = self._rows.get(canonical_id)
        if record is None:
            record = _FixtureRecord()
            self._rows[canonical_id] = record
        if not observation.evaluated:
            previous = record.lane_observation(observation.scan_lane)
            if previous is not None and previous.evaluated:
                record.leftover_this_pass = True
                if observation.source_events and not previous.source_events:
                    record.set_source_events(observation.source_events)
                return
        record.set_lane(observation)
        record.leftover_this_pass = not observation.evaluated

    def _drop_identity(self, canonical_id: str) -> None:
        self._rows.pop(canonical_id, None)
        for alias, target in list(self._aliases.items()):
            if target == canonical_id:
                self._aliases.pop(alias, None)


@dataclass
class _FixtureRecord:
    hot: LaneObservation | None = None
    universe: LaneObservation | None = None
    leftover_this_pass: bool = False
    extra_source_events: tuple[StoredSourceEvent, ...] = ()

    def set_lane(self, observation: LaneObservation) -> None:
        if observation.scan_lane is ScanLane.HOT:
            self.hot = observation
        else:
            self.universe = observation
        if observation.source_events:
            self.extra_source_events = observation.source_events

    def set_source_events(self, events: tuple[StoredSourceEvent, ...]) -> None:
        self.extra_source_events = events

    def lane_observation(self, lane: ScanLane) -> LaneObservation | None:
        if lane is ScanLane.HOT:
            return self.hot
        if lane is ScanLane.UNIVERSE:
            return self.universe
        return None

    def selected_observation(self, membership: ScanLane) -> LaneObservation | None:
        if membership is ScanLane.HOT:
            if self.hot is not None and self.hot.evaluated:
                return self.hot
            return None
        if self.universe is not None and self.universe.evaluated:
            return self.universe
        if self.hot is not None and self.hot.evaluated:
            return self.hot
        return None

    def display_fixture(self) -> DiscoveredFixture | None:
        for item in (self.hot, self.universe):
            if item is not None:
                fixture = item.fixture
                if self.leftover_this_pass and item.evaluated:
                    return fixture.model_copy(
                        update={
                            "market_evaluation_reason": item.fixture.market_evaluation_reason
                            or MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value,
                        }
                    )
                return fixture
        return None

    def display_markets(self) -> list[FixtureMarketInventoryRow]:
        for item in (self.hot, self.universe):
            if item is not None and item.evaluated:
                return list(item.markets)
        for item in (self.hot, self.universe):
            if item is not None:
                return list(item.markets)
        return []

    def source_events(self) -> tuple[StoredSourceEvent, ...]:
        for item in (self.hot, self.universe):
            if item is not None and item.source_events:
                return item.source_events
        return self.extra_source_events


def _stamp_fixture(
    fixture: DiscoveredFixture,
    *,
    lane: ScanLane,
    last_scanned_at: datetime,
) -> DiscoveredFixture:
    return fixture.model_copy(
        update={
            "scan_lane": lane.value,
            "last_scanned_at": last_scanned_at,
            "last_seen_at": last_scanned_at,
        }
    )


def _paper_market_ids_by_fixture(report: CollectionReport) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    fixtures = {item.canonical_event_id for item in report.discovered_fixtures}
    for decision in report.paper_decisions:
        market_id = (decision.canonical_market_id or "").strip()
        if not market_id:
            continue
        cluster_id = (decision.fixture_canonical_event_id or "").strip()
        decision_id = (decision.canonical_event_id or "").strip()
        target = cluster_id if cluster_id in fixtures else decision_id
        if not target:
            if len(fixtures) == 1:
                target = next(iter(fixtures))
            else:
                continue
        grouped.setdefault(target, [])
        if market_id not in grouped[target]:
            grouped[target].append(market_id)
    return {key: tuple(values) for key, values in grouped.items()}


def _source_events_from_report(
    report: CollectionReport,
    fixtures: dict[str, DiscoveredFixture],
) -> dict[str, tuple[StoredSourceEvent, ...]]:
    payload = getattr(report, "fixture_source_events", {}) or {}
    result: dict[str, tuple[StoredSourceEvent, ...]] = {}
    for canonical_id, rows in payload.items():
        if canonical_id not in fixtures:
            continue
        events: list[StoredSourceEvent] = []
        for row in rows:
            if isinstance(row, StoredSourceEvent):
                events.append(row)
                continue
            if not isinstance(row, dict):
                continue
            venue_raw = row.get("venue")
            source_id = str(row.get("source_event_id") or "").strip()
            raw = row.get("raw")
            if not venue_raw or not source_id or not isinstance(raw, dict):
                continue
            events.append(
                StoredSourceEvent(
                    venue=VenueName(venue_raw) if not isinstance(venue_raw, VenueName) else venue_raw,
                    source_event_id=source_id,
                    raw=raw,
                )
            )
        if events:
            result[canonical_id] = tuple(events)
    for fixture in fixtures.values():
        if fixture.canonical_event_id in result:
            continue
        source_id = str(fixture.source_event_id).strip()
        if not source_id:
            continue
        result[fixture.canonical_event_id] = (
            StoredSourceEvent(
                venue=fixture.source,
                source_event_id=source_id,
                raw={"id": source_id},
            ),
        )
    return result


def _aliases_from_report(
    report: CollectionReport,
    fixtures: dict[str, DiscoveredFixture],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for fixture in fixtures.values():
        aliases[fixture.canonical_event_id] = fixture.canonical_event_id
        source_id = str(fixture.source_event_id).strip()
        if source_id:
            aliases[source_id] = fixture.canonical_event_id
    for alias, canonical_id in report.fixture_identity_aliases.items():
        key = alias.strip()
        target = canonical_id.strip()
        if key and target in fixtures:
            aliases[key] = target
    for decision in report.paper_decisions:
        _alias_decision(aliases, decision, fixtures)
    return aliases


def _alias_decision(
    aliases: dict[str, str],
    decision: PaperScanDecision,
    fixtures: dict[str, DiscoveredFixture],
) -> None:
    cluster_id = (decision.fixture_canonical_event_id or "").strip()
    decision_id = (decision.canonical_event_id or "").strip()
    if cluster_id and cluster_id in fixtures:
        aliases[cluster_id] = cluster_id
        if decision_id:
            aliases[decision_id] = cluster_id
        return
    if decision_id and decision_id in fixtures:
        aliases[decision_id] = decision_id
