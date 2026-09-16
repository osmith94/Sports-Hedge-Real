from __future__ import annotations

import threading
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
from sports_hedge.application.current_market_inventory import (
    CurrentMarketSlot,
    apply_current_market_inventory,
    combined_radar_freshness,
    current_slots_prove_qualifying_opportunity,
    merge_current_market_slots,
    prune_expired_market_slots,
    stamp_current_market_row,
    union_paper_market_ids,
)
from sports_hedge.application.fixture_inventory import sort_fixture_inventory_rows
from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.application.scan_lanes import (
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
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
    is_explicit_terminal,
    is_trusted_lifecycle_correction,
    lifecycle_status_source,
    next_due_at,
    terminal_eviction_reason,
)
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


@dataclass(frozen=True)
class CurrentStateTombstone:
    """Current-radar eviction record. Not a fabricated completed/live label."""

    canonical_event_id: str
    aliases: frozenset[str]
    reason: str
    provider_status: str | None
    source: str | None
    observed_at: datetime


class FixtureCurrentStateStore:
    """Process-memory canonical fixture current-state (v1).

    Coordinator-owned. Dual-cadence HOT/UNIVERSE lanes upsert into this same
    store with TTL merge. One identity map; no second canonical system.

    Sequential re-anchor / alias convergence MERGES into the existing current
    row when trusted source IDs prove continuity. A later Polymarket-only
    cluster of the same match must not fork a second `evt:` identity or leave
    the abandoned Matchbook-anchored row opportunity-promoted. If two live rows
    later present overlapping trusted source/canonical IDs, they converge into
    one identity; aliases cannot steal another live fixture. Ambiguous
    opponent/source evidence fails closed and does not fuzzy-join fixtures.

    Membership and displayed provider status use the freshest observation by
    last_scanned_at. Each fixture owns a merged current market inventory across
    lanes: a HOT subset refresh updates only the canonical markets it evaluated
    and must not delete still-current equivalents from the other lane.
    Fresher HOT state supersedes older UNIVERSE state for the same canonical
    market. `not_evaluated` / provider-unavailable does not clobber prior
    still-valid market rows. An evaluated observation with no comparable
    current market records explicit evaluated-absence so stale proving slots
    cannot survive as omitted rows. Older lane snapshots cannot install into
    an empty lane when a newer status observation already exists.
    Expired or explicitly re-evaluated invalid rows leave/update truthfully.

    Fixture equivalent/qualifying/near counts and best/headline fields derive
    from that merged current inventory. Radar TTL may keep rows visible;
    paper eligibility / auto-capture still require executable quote freshness.

    HOT identity is the union of lifecycle HOT membership (in-play / <=60m
    pre-kickoff / bounded post-kickoff unknown) and current fixtures whose
    latest valid merged current-state proves a qualifying executable arb
    (Issue #200). Opportunity promotion is not a second identity store and
    does not change `classify_scan_lane`. Promotion disappears when the
    merged current-state ceases to qualify unless another lifecycle HOT
    reason still applies.

    Terminal tombstones keep explicit finished/completed/settled truth from
    resurrecting via a later stale UNIVERSE or other-venue unknown snapshot.
    Ingest tombstones retain previously bound cross-venue aliases and source
    IDs even when the terminal observation is a source subset, so a later
    non-authoritative venue cannot recreate the same fixture. Audit/history is
    not stored here and is not deleted.

    Do not fuzzy-match fixture names. Do not fabricate demo fixtures.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._rows: dict[str, _FixtureRecord] = {}
        self._aliases: dict[str, str] = {}
        self._tombstones: dict[str, CurrentStateTombstone] = {}
        self._tombstone_aliases: dict[str, str] = {}
        self._has_collection = False

    def clear(self, *, keep_tombstones: bool = False) -> None:
        with self._lock:
            self._generation = 0
            self._rows = {}
            self._aliases = {}
            self._has_collection = False
            if not keep_tombstones:
                self._tombstones = {}
                self._tombstone_aliases = {}

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def replace_from_report(self, report: CollectionReport) -> None:
        """Compatibility generation replace used by explicit diagnostic collects."""

        self.clear(keep_tombstones=True)
        self.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE)

    def upsert_from_report(
        self,
        report: CollectionReport,
        *,
        scan_lane: ScanLane | str = ScanLane.UNIVERSE,
        now: datetime | None = None,
    ) -> None:
        with self._lock:
            self._upsert_from_report_unlocked(
                report, scan_lane=scan_lane, now=now
            )

    def _upsert_from_report_unlocked(
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
        merge_map: dict[str, str] = {}
        for canonical_id, fixture in fixtures.items():
            aliases = _aliases_for_canonical(canonical_id, incoming_aliases)
            if self._reject_or_tombstone_incoming(
                canonical_id,
                fixture,
                aliases=aliases,
                scanned_at=scanned_at,
            ):
                continue
            target_id = self._merge_target_identity(canonical_id, aliases)
            merge_map[canonical_id] = target_id
            membership = classify_scan_lane(fixture, scanned_at)
            if membership is ScanLane.DROP:
                if is_explicit_terminal(fixture):
                    self._record_tombstone(
                        target_id,
                        fixture,
                        aliases=aliases | {canonical_id, target_id},
                        scanned_at=scanned_at,
                    )
                else:
                    self._drop_identity(target_id)
                    if target_id != canonical_id:
                        self._drop_identity(canonical_id)
                continue
            evaluated = (
                fixture.market_evaluation_state == MarketEvaluationState.EVALUATED.value
            )
            stored_fixture = fixture
            if target_id != canonical_id:
                stored_fixture = fixture.model_copy(update={"canonical_event_id": target_id})
            observation = LaneObservation(
                fixture=_stamp_fixture(stored_fixture, lane=lane, last_scanned_at=scanned_at),
                markets=list(markets.get(canonical_id) or markets.get(target_id, [])),
                scan_lane=lane,
                last_scanned_at=scanned_at,
                paper_market_ids=paper_ids.get(canonical_id) or paper_ids.get(target_id, ()),
                source_events=source_events.get(canonical_id) or source_events.get(target_id, ()),
                evaluated=evaluated,
            )
            self._upsert_observation(target_id, observation)
            self._bind_aliases(aliases | {canonical_id, target_id}, target_id)
            for event in observation.source_events:
                self._bind_alias(event.source_event_id, target_id)
        for alias, incoming_id in incoming_aliases.items():
            target = merge_map.get(incoming_id, incoming_id)
            if target in self._rows:
                self._bind_alias(alias, target)
        for alias, canonical_id in list(self._aliases.items()):
            if canonical_id not in self._rows:
                self._aliases.pop(alias, None)
        self._generation += 1
        self._has_collection = True

    def resolve_canonical_id(self, identity: str) -> str | None:
        with self._lock:
            return self._resolve_canonical_id_unlocked(identity)

    def _resolve_canonical_id_unlocked(self, identity: str) -> str | None:
        wanted = identity.strip()
        if not wanted:
            return None
        if self._tombstone_for_unlocked(wanted) is not None:
            return None
        canonical_id = self._aliases.get(wanted)
        if canonical_id is None and wanted in self._rows:
            return wanted
        if canonical_id is None or canonical_id not in self._rows:
            return None
        return canonical_id

    def identities_for(self, identity: str) -> frozenset[str]:
        with self._lock:
            canonical_id = self._resolve_canonical_id_unlocked(identity)
            if canonical_id is None:
                return frozenset()
            aliases = {
                alias for alias, target in self._aliases.items() if target == canonical_id
            }
            aliases.add(canonical_id)
            return frozenset(aliases)

    def detail(
        self,
        identity: str,
        now: datetime | None = None,
        **kwargs: Any,
    ) -> FixtureDetailReadModel | None:
        with self._lock:
            if self._tombstone_for_unlocked(identity) is not None:
                return None
            if now is not None:
                self._evict_non_current(now, **kwargs)
            canonical_id = self._resolve_canonical_id_unlocked(identity)
            if canonical_id is None:
                return None
            record = self._rows[canonical_id]
            record.prune_markets(now, **kwargs)
            displayed = record.status_fixture(now, **kwargs)
            if displayed is None:
                return None
            if now is not None and classify_scan_lane(
                displayed, now, **_classify_kwargs(kwargs)
            ) is ScanLane.DROP:
                self._drop_identity(canonical_id)
                return None
            return FixtureDetailReadModel(
                fixture=displayed,
                markets=list(record.display_markets(now, **kwargs)),
            )

    def tombstone_for(self, identity: str) -> CurrentStateTombstone | None:
        with self._lock:
            return self._tombstone_for_unlocked(identity)

    def _tombstone_for_unlocked(self, identity: str) -> CurrentStateTombstone | None:
        wanted = identity.strip()
        if not wanted:
            return None
        canonical_id = self._tombstone_aliases.get(wanted, wanted)
        return self._tombstones.get(canonical_id)

    def has_collection(self) -> bool:
        with self._lock:
            return self._has_collection

    def inventory(
        self,
        now: datetime,
        *,
        hot_horizon=DEFAULT_HOT_HORIZON,
        post_kickoff_unknown_horizon=DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
        hot_interval_seconds: int = DEFAULT_HOT_INTERVAL_SECONDS,
        universe_interval_seconds: int = DEFAULT_UNIVERSE_INTERVAL_SECONDS,
        hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
        max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    ) -> list[DiscoveredFixture]:
        with self._lock:
            market_kwargs = {
                "hot_ttl_seconds": hot_ttl_seconds,
                "universe_ttl_seconds": universe_ttl_seconds,
                "max_quote_age_ms": max_quote_age_ms,
            }
            self._evict_non_current(
                now,
                hot_horizon=hot_horizon,
                post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
                **market_kwargs,
            )
            rows: list[DiscoveredFixture] = []
            for record in list(self._rows.values()):
                record.prune_markets(now, **market_kwargs)
                fixture = record.status_fixture(now, **market_kwargs)
                if fixture is None:
                    continue
                membership = self._identity_membership(
                    record,
                    fixture,
                    now,
                    classify_kwargs={
                        "hot_horizon": hot_horizon,
                        "post_kickoff_unknown_horizon": post_kickoff_unknown_horizon,
                    },
                    market_kwargs=market_kwargs,
                )
                if membership is ScanLane.DROP:
                    continue
                lane, scanned = record.scheduler_lane_scan(membership, fixture)
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
        with self._lock:
            return self._current_radar_rows_unlocked(
                now,
                hot_horizon=hot_horizon,
                post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
                hot_ttl_seconds=hot_ttl_seconds,
                universe_ttl_seconds=universe_ttl_seconds,
                hot_interval_seconds=hot_interval_seconds,
                universe_interval_seconds=universe_interval_seconds,
                quote_age_ms_by_market=quote_age_ms_by_market,
                max_quote_age_ms=max_quote_age_ms,
            )

    def _current_radar_rows_unlocked(
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
        market_kwargs = {
            "hot_ttl_seconds": hot_ttl_seconds,
            "universe_ttl_seconds": universe_ttl_seconds,
            "max_quote_age_ms": max_quote_age_ms,
        }
        self._evict_non_current(
            evaluated,
            hot_horizon=hot_horizon,
            post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
            **market_kwargs,
        )
        for canonical_id, record in self._rows.items():
            record.prune_markets(evaluated, **market_kwargs)
            fixture = record.status_fixture(evaluated, **market_kwargs)
            if fixture is None:
                continue
            membership = self._identity_membership(
                record,
                fixture,
                evaluated,
                classify_kwargs={
                    "hot_horizon": hot_horizon,
                    "post_kickoff_unknown_horizon": post_kickoff_unknown_horizon,
                },
                market_kwargs=market_kwargs,
            )
            if membership is ScanLane.DROP:
                continue
            markets = record.display_markets(evaluated, **market_kwargs)
            paper_ids = record.radar_paper_market_ids(membership, evaluated, **market_kwargs)
            observation = record.selected_observation(membership)
            if markets:
                freshness = combined_radar_freshness(markets)
                if freshness == FRESHNESS_EXPIRED:
                    continue
                lane, scanned = record.scheduler_lane_scan(membership, fixture)
                radar_fixture = fixture
                source_events = record.source_events()
            else:
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
                lane = observation.scan_lane
                scanned = observation.last_scanned_at
                radar_fixture = observation.fixture
                source_events = observation.source_events
                paper_ids = observation.paper_market_ids
            if not paper_ids and not markets:
                continue
            current.append(
                FixtureRadarRow(
                    canonical_event_id=canonical_id,
                    fixture=radar_fixture,
                    markets=list(markets),
                    membership=membership,
                    observation_lane=lane,
                    last_scanned_at=scanned,
                    paper_market_ids=paper_ids,
                    freshness=freshness,
                    next_due_at=next_due_at(
                        scanned,
                        lane,
                        hot_interval_seconds=hot_interval_seconds,
                        universe_interval_seconds=universe_interval_seconds,
                    ),
                    source_events=source_events,
                )
            )
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
        with self._lock:
            evaluated = require_aware_instant(now, "now")
            classify_kwargs = _classify_kwargs(kwargs)
            market_kwargs = _market_ttl_kwargs(kwargs)
            self._evict_non_current(evaluated, **classify_kwargs, **market_kwargs)
            fixtures: list[DiscoveredFixture] = []
            for record in list(self._rows.values()):
                record.prune_markets(evaluated, **market_kwargs)
                fixture = record.status_fixture(evaluated, **market_kwargs)
                if fixture is None:
                    continue
                membership = self._identity_membership(
                    record,
                    fixture,
                    evaluated,
                    classify_kwargs=classify_kwargs,
                    market_kwargs=market_kwargs,
                )
                if membership is ScanLane.HOT:
                    if current_slots_prove_qualifying_opportunity(
                        record.live_market_slots(),
                        now=evaluated,
                        **market_kwargs,
                    ):
                        fixture = fixture.model_copy(update={"solver_is_arbitrage": True})
                    fixtures.append(fixture)
            fixtures.sort(key=hot_sort_key)
            return [item.canonical_event_id for item in fixtures]

    def known_source_events(self, canonical_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
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
        with self._lock:
            classify_kwargs = _classify_kwargs(kwargs)
            market_kwargs = _market_ttl_kwargs(kwargs)
            self._evict_non_current(now, **classify_kwargs, **market_kwargs)
            hot = 0
            universe = 0
            for record in list(self._rows.values()):
                record.prune_markets(now, **market_kwargs)
                fixture = record.status_fixture(now, **market_kwargs)
                if fixture is None:
                    continue
                membership = self._identity_membership(
                    record,
                    fixture,
                    now,
                    classify_kwargs=classify_kwargs,
                    market_kwargs=market_kwargs,
                )
                if membership is ScanLane.HOT:
                    hot += 1
                elif membership is ScanLane.UNIVERSE:
                    universe += 1
            return hot, universe

    def _identity_membership(
        self,
        record: _FixtureRecord,
        fixture: DiscoveredFixture,
        now: datetime,
        *,
        classify_kwargs: dict[str, Any],
        market_kwargs: dict[str, Any],
    ) -> ScanLane:
        """HOT identity = lifecycle HOT or current qualifying-opportunity promotion.

        `classify_scan_lane` remains the lifecycle classifier. Promotion reads
        merged current-state market truth, not UI labels or historical audit.
        """

        lifecycle = classify_scan_lane(fixture, now, **classify_kwargs)
        if lifecycle is ScanLane.DROP or lifecycle is ScanLane.HOT:
            return lifecycle
        if current_slots_prove_qualifying_opportunity(
            record.live_market_slots(),
            now=now,
            **market_kwargs,
        ):
            return ScanLane.HOT
        return lifecycle

    def _merge_target_identity(self, canonical_id: str, aliases: set[str]) -> str:
        """Reuse the live row proved by trusted source/canonical aliases.

        Incoming aliases may converge several already-live rows of one fixture
        when trusted source/canonical IDs overlap. A cluster cannot steal another
        live fixture without those overlapping source IDs.
        """

        incoming_ids = {item for item in (aliases | {canonical_id}) if item}
        live_targets = self._live_targets_for_aliases(incoming_ids)
        if canonical_id in self._rows:
            live_targets.add(canonical_id)
        mergeable = self._mergeable_identities(live_targets, incoming_ids)
        if len(mergeable) > 1:
            tight = {
                item
                for item in mergeable
                if not (self._recorded_source_ids(item) - incoming_ids)
            }
            if len(tight) > 1:
                survivor = self._choose_survivor(tight, incoming_ids, canonical_id)
                for item in tight:
                    if item != survivor:
                        self._absorb_live_identity(item, survivor)
                return survivor
            if len(tight) == 1:
                survivor = next(iter(tight))
                if canonical_id in self._rows and canonical_id != survivor:
                    incoming_extra = self._recorded_source_ids(canonical_id) - incoming_ids
                    if not incoming_extra:
                        self._absorb_live_identity(canonical_id, survivor)
                return survivor
            return canonical_id
        if canonical_id in self._rows:
            return canonical_id
        existing = self._aliases.get(canonical_id)
        if existing is not None and existing in self._rows:
            return existing
        if len(mergeable) != 1:
            return canonical_id
        target = next(iter(mergeable))
        if not self._has_trusted_source_overlap(target, incoming_ids):
            return canonical_id
        return target

    def _live_targets_for_aliases(self, aliases: set[str]) -> set[str]:
        targets: set[str] = set()
        for alias in aliases:
            if self._tombstone_for_unlocked(alias) is not None:
                continue
            resolved = self._aliases.get(alias)
            if resolved is not None and resolved in self._rows:
                targets.add(resolved)
            elif alias in self._rows:
                targets.add(alias)
        return targets

    def _recorded_source_ids(self, target_id: str) -> set[str]:
        record = self._rows.get(target_id)
        if record is None:
            return set()
        return {
            event.source_event_id
            for event in record.source_events()
            if event.source_event_id
        }

    def _mergeable_identities(self, live_targets: set[str], incoming_ids: set[str]) -> set[str]:
        return {
            target
            for target in live_targets
            if self._has_trusted_source_overlap(target, incoming_ids)
        }

    def _choose_survivor(
        self,
        candidates: set[str],
        incoming_ids: set[str],
        incoming_canonical: str,
    ) -> str:
        scored: list[tuple[int, int, str]] = []
        for item in candidates:
            score = len(incoming_ids & self._identity_membership_keys(item))
            incoming_penalty = 1 if item == incoming_canonical else 0
            scored.append((-score, incoming_penalty, item))
        scored.sort()
        return scored[0][2]

    def _identity_membership_keys(self, target_id: str) -> set[str]:
        existing: set[str] = {target_id}
        record = self._rows.get(target_id)
        if record is not None:
            for event in record.source_events():
                if event.source_event_id:
                    existing.add(event.source_event_id)
            status = record.status_observation()
            if status is not None:
                source_id = str(status.fixture.source_event_id or "").strip()
                if source_id:
                    existing.add(source_id)
                canonical = str(status.fixture.canonical_event_id or "").strip()
                if canonical:
                    existing.add(canonical)
        for alias, dest in self._aliases.items():
            if dest == target_id:
                existing.add(alias)
        return {item for item in existing if item}

    def _has_trusted_source_overlap(self, target_id: str, incoming_ids: set[str]) -> bool:
        return bool({item for item in incoming_ids if item} & self._identity_membership_keys(target_id))

    def _absorb_live_identity(self, source_id: str, target_id: str) -> None:
        """Merge one live row into another. Incoming canonical becomes an alias."""

        if source_id == target_id:
            return
        source = self._rows.get(source_id)
        target = self._rows.get(target_id)
        if source is None or target is None:
            return
        target.absorb_record(source, target_id=target_id)
        for alias, dest in list(self._aliases.items()):
            if dest == source_id:
                self._aliases[alias] = target_id
        self._aliases[source_id] = target_id
        self._rows.pop(source_id, None)

    def _bind_alias(self, alias: str, target_id: str) -> None:
        key = alias.strip()
        if not key or target_id not in self._rows:
            return
        current = self._aliases.get(key)
        if current is not None and current in self._rows and current != target_id:
            return
        self._aliases[key] = target_id

    def _bind_aliases(self, aliases: set[str], target_id: str) -> None:
        for alias in aliases:
            self._bind_alias(alias, target_id)

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
            if record.set_lane(observation):
                record.leftover_this_pass = True
            return
        if record.set_lane(observation):
            record.merge_markets(observation)
            record.leftover_this_pass = False

    def _evict_non_current(self, now: datetime, **kwargs: Any) -> None:
        classify_kwargs = _classify_kwargs(kwargs)
        market_kwargs = _market_ttl_kwargs(kwargs)
        evaluated = require_aware_instant(now, "now")
        for canonical_id, record in list(self._rows.items()):
            record.prune_markets(evaluated, **market_kwargs)
            fixture = record.status_fixture(evaluated, **market_kwargs)
            if fixture is None:
                self._drop_identity(canonical_id)
                continue
            membership = classify_scan_lane(fixture, evaluated, **classify_kwargs)
            if membership is ScanLane.DROP:
                if is_explicit_terminal(fixture):
                    aliases = {
                        alias
                        for alias, target in self._aliases.items()
                        if target == canonical_id
                    }
                    aliases.add(canonical_id)
                    self._record_tombstone(
                        canonical_id,
                        fixture,
                        aliases=aliases,
                        scanned_at=evaluated,
                    )
                else:
                    self._drop_identity(canonical_id)

    def _reject_or_tombstone_incoming(
        self,
        canonical_id: str,
        fixture: DiscoveredFixture,
        *,
        aliases: set[str],
        scanned_at: datetime,
    ) -> bool:
        tombstone = self._tombstone_for_unlocked(canonical_id)
        if tombstone is None:
            for alias in aliases:
                tombstone = self._tombstone_for_unlocked(alias)
                if tombstone is not None:
                    break
        if tombstone is None:
            return False
        if is_trusted_lifecycle_correction(
            fixture,
            observed_at=scanned_at,
            tombstone_observed_at=tombstone.observed_at,
            tombstone_reason=tombstone.reason,
            tombstone_source=tombstone.source,
        ):
            self._clear_tombstone(tombstone.canonical_event_id)
            return False
        extra = set(aliases)
        extra.add(canonical_id)
        live_to_bury: set[str] = set()
        if canonical_id in self._rows:
            live_to_bury.add(canonical_id)
        bound = self._aliases.get(canonical_id)
        if bound is not None and bound in self._rows:
            live_to_bury.add(bound)
        for live_id in live_to_bury:
            extra.update(self._identity_membership_keys(live_id))
        if is_explicit_terminal(fixture) and scanned_at >= tombstone.observed_at:
            self._record_tombstone(
                tombstone.canonical_event_id,
                fixture,
                aliases=extra | set(tombstone.aliases),
                scanned_at=scanned_at,
            )
        else:
            self._extend_tombstone_aliases(tombstone, extra)
        for live_id in live_to_bury:
            if live_id in self._rows:
                self._drop_identity(live_id)
        return True

    def _extend_tombstone_aliases(
        self, tombstone: CurrentStateTombstone, extra: set[str]
    ) -> None:
        merged = {item for item in (set(tombstone.aliases) | extra | {tombstone.canonical_event_id}) if item}
        updated = CurrentStateTombstone(
            canonical_event_id=tombstone.canonical_event_id,
            aliases=frozenset(merged),
            reason=tombstone.reason,
            provider_status=tombstone.provider_status,
            source=tombstone.source,
            observed_at=tombstone.observed_at,
        )
        self._tombstones[tombstone.canonical_event_id] = updated
        for alias in updated.aliases:
            self._tombstone_aliases[alias] = tombstone.canonical_event_id

    def _record_tombstone(
        self,
        canonical_id: str,
        fixture: DiscoveredFixture,
        *,
        aliases: set[str],
        scanned_at: datetime,
    ) -> None:
        existing = self._tombstones.get(canonical_id)
        merged = set(aliases)
        merged.add(canonical_id)
        if existing is not None:
            merged.update(existing.aliases)
        live_id = canonical_id
        if live_id not in self._rows:
            resolved = self._aliases.get(canonical_id)
            if resolved is not None and resolved in self._rows:
                live_id = resolved
        merged.update(self._identity_membership_keys(live_id))
        status_source = lifecycle_status_source(fixture)
        tombstone = CurrentStateTombstone(
            canonical_event_id=canonical_id,
            aliases=frozenset(merged),
            reason=terminal_eviction_reason(fixture),
            provider_status=fixture.fixture_status,
            source=status_source,
            observed_at=scanned_at,
        )
        self._tombstones[canonical_id] = tombstone
        for alias in tombstone.aliases:
            self._tombstone_aliases[alias] = canonical_id
        self._drop_identity(canonical_id)

    def _clear_tombstone(self, canonical_id: str) -> None:
        tombstone = self._tombstones.pop(canonical_id, None)
        if tombstone is None:
            return
        for alias in tombstone.aliases:
            if self._tombstone_aliases.get(alias) == canonical_id:
                self._tombstone_aliases.pop(alias, None)

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
    markets: dict[str, CurrentMarketSlot] | None = None

    def set_lane(self, observation: LaneObservation) -> bool:
        current = self.hot if observation.scan_lane is ScanLane.HOT else self.universe
        if current is not None and observation.last_scanned_at < current.last_scanned_at:
            if observation.source_events:
                self.extra_source_events = _merge_source_events(
                    self.extra_source_events, observation.source_events
                )
            return False
        if current is None:
            status = self.status_observation()
            if status is not None and observation.last_scanned_at < status.last_scanned_at:
                if observation.source_events:
                    self.extra_source_events = _merge_source_events(
                        self.extra_source_events, observation.source_events
                    )
                return False
        if observation.scan_lane is ScanLane.HOT:
            self.hot = observation
        else:
            self.universe = observation
        if observation.source_events:
            self.extra_source_events = _merge_source_events(
                self.extra_source_events, observation.source_events
            )
        return True

    def merge_markets(self, observation: LaneObservation) -> None:
        self.markets = merge_current_market_slots(
            self.markets or {},
            list(observation.markets),
            scan_lane=observation.scan_lane,
            scanned_at=observation.last_scanned_at,
            paper_market_ids=observation.paper_market_ids,
            evaluated=observation.evaluated,
        )

    def prune_markets(self, now: datetime | None, **kwargs: Any) -> None:
        if now is None or not self.markets:
            return
        ttl = _market_ttl_kwargs(kwargs)
        self.markets = prune_expired_market_slots(self.markets, now, **ttl)

    def set_source_events(self, events: tuple[StoredSourceEvent, ...]) -> None:
        self.extra_source_events = _merge_source_events(self.extra_source_events, events)

    def absorb_record(self, other: _FixtureRecord, *, target_id: str) -> None:
        """Keep newer lane clocks and market slots from another live identity."""

        self.extra_source_events = _merge_source_events(
            self.extra_source_events,
            _merge_source_events(other.extra_source_events, other.source_events()),
        )
        for observation in (other.universe, other.hot):
            if observation is None:
                continue
            rewritten = LaneObservation(
                fixture=observation.fixture.model_copy(
                    update={"canonical_event_id": target_id}
                ),
                markets=list(observation.markets),
                scan_lane=observation.scan_lane,
                last_scanned_at=observation.last_scanned_at,
                paper_market_ids=observation.paper_market_ids,
                source_events=observation.source_events,
                evaluated=observation.evaluated,
            )
            self.set_lane(rewritten)
        if other.markets:
            if self.markets is None:
                self.markets = dict(other.markets)
            else:
                for key, slot in other.markets.items():
                    previous = self.markets.get(key)
                    if previous is None or slot.last_scanned_at >= previous.last_scanned_at:
                        self.markets[key] = slot
        self.leftover_this_pass = self.leftover_this_pass or other.leftover_this_pass

    def lane_observation(self, lane: ScanLane) -> LaneObservation | None:
        if lane is ScanLane.HOT:
            return self.hot
        if lane is ScanLane.UNIVERSE:
            return self.universe
        return None

    def selected_observation(self, membership: ScanLane) -> LaneObservation | None:
        """Lane economics for Tracked. HOT membership never falls back to UNIVERSE prices."""

        if membership is ScanLane.HOT:
            if self.hot is not None and self.hot.evaluated:
                return self.hot
            return None
        if self.universe is not None and self.universe.evaluated:
            return self.universe
        if self.hot is not None and self.hot.evaluated:
            return self.hot
        return None

    def status_observation(self) -> LaneObservation | None:
        """Freshest provider-status snapshot. Does not prefer HOT over a newer UNIVERSE."""

        candidates = [item for item in (self.hot, self.universe) if item is not None]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                item.last_scanned_at,
                1 if item.scan_lane is ScanLane.UNIVERSE else 0,
            ),
        )

    def live_market_slots(self) -> list[CurrentMarketSlot]:
        return [
            slot
            for slot in (self.markets or {}).values()
            if not slot.evaluated_absent
        ]

    def scheduler_lane_scan(
        self, membership: ScanLane, fixture: DiscoveredFixture
    ) -> tuple[ScanLane, datetime]:
        """HOT/UNIVERSE due times follow membership, not the latest retained market slot."""

        lane_obs = self.lane_observation(membership)
        if lane_obs is not None:
            return membership, lane_obs.last_scanned_at
        selected = self.selected_observation(membership)
        if selected is not None:
            return selected.scan_lane, selected.last_scanned_at
        return membership, fixture.last_seen_at

    def radar_paper_market_ids(
        self,
        membership: ScanLane,
        now: datetime | None = None,
        **kwargs: Any,
    ) -> tuple[str, ...]:
        del now
        from_slots = union_paper_market_ids(self.live_market_slots())
        if from_slots:
            return from_slots
        observation = self.selected_observation(membership)
        if observation is None or not observation.evaluated:
            return ()
        return observation.paper_market_ids

    def status_fixture(self, now: datetime | None = None, **kwargs: Any) -> DiscoveredFixture | None:
        observation = self.status_observation()
        if observation is None:
            return None
        fixture = observation.fixture
        if self.leftover_this_pass and observation.evaluated:
            fixture = fixture.model_copy(
                update={
                    "market_evaluation_reason": observation.fixture.market_evaluation_reason
                    or MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value,
                }
            )
        slots = self.live_market_slots()
        if not slots:
            if self.markets is None:
                return fixture
            return fixture.model_copy(
                update={
                    "discovered_market_count": 0,
                    "matched_market_count": 0,
                    "matched_equivalent_count": 0,
                    "qualifying_market_count": 0,
                    "near_executable_market_count": 0,
                    "best_arb_market": None,
                    "solver_is_arbitrage": False,
                    "headline_band": "no_executable_arb",
                    "opportunity_state": "unmatched",
                    "current_net_edge": None,
                }
            )
        ttl = _market_ttl_kwargs(kwargs)
        projected, _rows = apply_current_market_inventory(fixture, slots, now=now, **ttl)
        return projected

    def display_fixture(self, now: datetime | None = None, **kwargs: Any) -> DiscoveredFixture | None:
        return self.status_fixture(now, **kwargs)

    def display_markets(self, now: datetime | None = None, **kwargs: Any) -> list[FixtureMarketInventoryRow]:
        ttl = _market_ttl_kwargs(kwargs)
        rows = [
            stamp_current_market_row(slot, now=now, **ttl)
            for slot in self.live_market_slots()
        ]
        return sort_fixture_inventory_rows(rows)

    def source_events(self) -> tuple[StoredSourceEvent, ...]:
        merged: dict[tuple[str, str], StoredSourceEvent] = {}
        for item in (self.universe, self.hot):
            if item is None:
                continue
            for event in item.source_events:
                merged[(event.venue.value, event.source_event_id)] = event
        for event in self.extra_source_events:
            key = (event.venue.value, event.source_event_id)
            if key not in merged:
                merged[key] = event
        return tuple(merged.values())


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


def _merge_source_events(
    existing: tuple[StoredSourceEvent, ...],
    incoming: tuple[StoredSourceEvent, ...],
) -> tuple[StoredSourceEvent, ...]:
    """Keep prior venue IDs when a later lane refresh omits a disabled venue."""

    merged: dict[tuple[str, str], StoredSourceEvent] = {}
    for event in existing:
        merged[(event.venue.value, event.source_event_id)] = event
    for event in incoming:
        merged[(event.venue.value, event.source_event_id)] = event
    return tuple(merged.values())


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
    for canonical_id, rows in (getattr(report, "fixture_source_events", {}) or {}).items():
        if canonical_id not in fixtures:
            continue
        for row in rows:
            if isinstance(row, StoredSourceEvent):
                source_id = row.source_event_id.strip()
            elif isinstance(row, dict):
                source_id = str(row.get("source_event_id") or "").strip()
            else:
                continue
            if source_id:
                aliases[source_id] = canonical_id
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


def _aliases_for_canonical(canonical_id: str, aliases: dict[str, str]) -> set[str]:
    found = {alias for alias, target in aliases.items() if target == canonical_id}
    found.add(canonical_id)
    return found


def _classify_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed = {}
    if "hot_horizon" in kwargs:
        allowed["hot_horizon"] = kwargs["hot_horizon"]
    if "post_kickoff_unknown_horizon" in kwargs:
        allowed["post_kickoff_unknown_horizon"] = kwargs["post_kickoff_unknown_horizon"]
    return allowed


def _market_ttl_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed: dict[str, Any] = {}
    if "hot_ttl_seconds" in kwargs:
        allowed["hot_ttl_seconds"] = kwargs["hot_ttl_seconds"]
    if "universe_ttl_seconds" in kwargs:
        allowed["universe_ttl_seconds"] = kwargs["universe_ttl_seconds"]
    if "max_quote_age_ms" in kwargs:
        allowed["max_quote_age_ms"] = kwargs["max_quote_age_ms"]
    return allowed
