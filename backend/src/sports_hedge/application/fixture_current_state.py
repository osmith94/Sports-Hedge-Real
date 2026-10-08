from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
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
    current_slots_net_proximity_distance_pp,
    current_slots_prove_qualifying_opportunity,
    current_slots_prove_surveillance_opportunity,
    merge_current_market_slots,
    prune_expired_market_slots,
    stamp_current_market_row,
    union_paper_market_ids,
)
from sports_hedge.application.event_loop_activity import TimedRLock
from sports_hedge.application.fixture_inventory import sort_fixture_inventory_rows
from sports_hedge.application.hot_identity import (
    hot_scheduling_key,
    unique_hot_scheduling_ids,
)
from sports_hedge.application.hot_market_relationships import (
    HotMarketRelationship,
    relationships_from_current_slots,
)
from sports_hedge.application.operations_read_model import (
    AWAITING_CROSS_VENUE_STATES,
    DeferredFixtureReport,
    DeferredFixtureRow,
    HotRosterEntry,
    UniverseCatalogueFixture,
    UniverseCatalogueMemory,
    UniverseCatalogueMetadata,
    UniverseCatalogueSnapshot,
)
from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.arbitrage.watchlist.economics import (
    DEFAULT_HOT_MINIMUM_LIMITING_DEPTH_GBP,
    DEFAULT_HOT_PROXIMITY_BAND_PP,
)
from sports_hedge.application.scan_lanes import (
    DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_HOT_HORIZON,
    DEFAULT_HOT_INTERVAL_SECONDS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
    DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
    DEFAULT_UNIVERSE_INTERVAL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF,
    FRESHNESS_EXPIRED,
    ScanLane,
    authoritative_post_kickoff_zero_equivalents,
    classify_scan_lane,
    explicit_matched_equivalent_count,
    freshness_class,
    hot_reason_labels,
    hot_sort_key,
    is_explicit_terminal,
    is_schedule_exception,
    is_trusted_lifecycle_correction,
    kickoff_has_passed,
    lifecycle_status_source,
    next_due_at,
    successful_complete_market_evaluation,
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
    universe_generation_id: int | None = None
    pricing_refresh: bool = False


@dataclass(frozen=True)
class _ConsoleProjection:
    as_of: datetime
    hot_count: int
    universe_count: int
    breakdown: tuple[int, int, int]
    hot_roster: list[HotRosterEntry]
    deferred_awaiting_count: int


@dataclass(frozen=True)
class OperatorBoard:
    """Fixture board captured at ``as_of`` and projected without the store lock."""

    as_of: datetime
    discovered: list[DiscoveredFixture]
    membership: tuple[int, int]
    breakdown: tuple[int, int, int]


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
class MarketPriceClock:
    """Discovery confirmation and the last actual price, kept apart.

    ``discovered_at`` is the evaluated UNIVERSE lane observation. BACKGROUND
    pricing does not move it. ``priced_at`` is a BACKGROUND price anchor or a
    HOT slot scan. A discovery observation is not reported as a price.
    ``price_lane`` is ``background``, ``hot``, or None when no price clock exists.
    """

    discovered_at: datetime | None
    priced_at: datetime | None
    price_lane: str | None


def _market_closure_may_restore(fixture: Any, now: datetime) -> bool:
    """A zero-equivalent closure is not a provider terminal tombstone.

    Restore when a later observation is provider-live, still has matched
    equivalents, returns to pre-kickoff, or is an explicit schedule exception.
    Incomplete scans do not restore and do not count as a new closure.
    """

    if is_explicit_terminal(fixture):
        return False
    if is_schedule_exception(fixture):
        return True
    if getattr(fixture, "in_running", None) is True:
        return True
    if successful_complete_market_evaluation(fixture):
        count = explicit_matched_equivalent_count(fixture)
        if count is not None and count > 0:
            return True
    if not kickoff_has_passed(fixture, now):
        kickoff = getattr(fixture, "kickoff_utc", None)
        if kickoff is not None:
            return True
    return False


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
    from that merged current inventory. Equivalent / ApprovedEquivalent
    presence is generation-aware: a UNIVERSE evaluation remains discovery
    current for the proving generation, then expires at that generation's
    own close plus radar TTL. A later open generation does not revive or
    suspend that expiry. A later authoritative re-evaluation may refresh
    or remove the relationship sooner. Radar TTL may keep quote-stale rows
    visible as relationship truth; paper eligibility / auto-capture / HOT
    promotion still require executable or radar-current quote freshness.

    HOT identity is the union of lifecycle HOT membership (provider in-play /
    <=60m pre-kickoff / bounded post-kickoff unknown, subject to the 4h hard
    post-kickoff current-radar ceiling) and current fixtures whose
    latest valid merged current-state proves a qualifying executable
    arb (Issue #200), net-proximity within 0.50pp of the operator Min Net
    Arb trigger, or already-triggered economics that are not yet
    executable-fresh (Tenet 19 / Issue #357). Opportunity promotion is not
    a second identity store and does not change `classify_scan_lane`.
    Promotion disappears when the merged current-state ceases to show
    triggered or in-band net economics unless another lifecycle HOT reason
    still applies. A stale in-running/open flag cannot keep a fixture HOT
    after the current-radar ceiling. Aliases of one football fixture
    collapse to one HOT scheduling unit.

    A post-kickoff fixture whose latest successful complete evaluation reports
    zero matched equivalents leaves current radar with
    ``no_current_equivalent_markets_post_kickoff``. That closure does not
    write ``completed`` or ``in_running``. Incomplete scans keep the last
    known equivalents. Operator IN PLAY for a post-kickoff row with remaining
    equivalents is a read-model label and does not overwrite provider
    ``in_running``.

    Terminal tombstones keep explicit finished/completed/settled truth from
    resurrecting via a later stale UNIVERSE or other-venue unknown snapshot.
    Ingest tombstones retain previously bound cross-venue aliases and source
    IDs even when the terminal observation is a source subset, so a later
    non-authoritative venue cannot recreate the same fixture. Audit/history is
    not stored here and is not deleted.

    Do not fuzzy-match fixture names. Do not fabricate demo fixtures.
    """

    def __init__(self) -> None:
        self._lock = TimedRLock("fixture_current_state")
        self._generation = 0
        self._reset_generation = 0
        self._rows: dict[str, _FixtureRecord] = {}
        self._aliases: dict[str, str] = {}
        self._tombstones: dict[str, CurrentStateTombstone] = {}
        self._tombstone_aliases: dict[str, str] = {}
        self._scheduling_index: dict[str, str] = {}
        self._has_collection = False
        self._open_universe_generation_id: int | None = None
        self._universe_generation_closed_at_by_id: dict[int, datetime] = {}
        self._execution_miss_hot_until: dict[str, datetime] = {}
        self._touched_ids: set[str] | None = None
        self._universe_catalogue = UniverseCatalogueMemory()
        self._hot_proximity_limits = (
            DEFAULT_HOT_PROXIMITY_BAND_PP,
            DEFAULT_HOT_MINIMUM_LIMITING_DEPTH_GBP,
        )

    def set_hot_proximity_limits(
        self,
        *,
        band_pp: Decimal,
        minimum_limiting_depth_gbp: Decimal,
    ) -> None:
        """Remember coordinator-resolved HOT proximity limits. No settings I/O."""

        self._hot_proximity_limits = (band_pp, minimum_limiting_depth_gbp)

    def clear(self, *, keep_tombstones: bool = False, keep_universe_generation: bool = False) -> None:
        with self._lock:
            self._reset_generation += 1
            self._generation = 0
            self._rows = {}
            self._aliases = {}
            self._scheduling_index = {}
            self._has_collection = False
            if not keep_tombstones:
                self._tombstones = {}
                self._tombstone_aliases = {}
            if not keep_universe_generation:
                self._open_universe_generation_id = None
                self._universe_generation_closed_at_by_id = {}
            self._execution_miss_hot_until = {}
            self._universe_catalogue.clear(seen_at=datetime.now(UTC))

    def drop_universe_working_set(
        self,
        now: datetime,
        *,
        keep_canonical_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        """Drop live UNIVERSE current-state rows without wiping HOT/ACTIVE identity.

        Does not increment ``reset_generation`` (HOT/BACKGROUND/ACTIVE
        projections must still commit). Tombstones, aliases of retained HOT
        rows, and audit/history stored elsewhere are untouched. Open PAPER
        trades may disappear from the UNIVERSE board while remaining
        ACTIVE-managed via catalogue/native IDs.
        """

        keep = {item for item in (keep_canonical_ids or set()) if item}
        evaluated = require_aware_instant(now, "now")
        classify_kwargs = {
            "hot_horizon": DEFAULT_HOT_HORIZON,
            "post_kickoff_unknown_horizon": DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
            "post_kickoff_current_radar_ceiling": DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
        }
        with self._lock:
            self._open_universe_generation_id = None
            self._universe_generation_closed_at_by_id = {}
            for canonical_id, record in list(self._rows.items()):
                aliases = self._identity_membership_keys(canonical_id)
                # Open PAPER / keep IDs stay ACTIVE-managed even when the UNIVERSE
                # radar row is dropped. Lifecycle HOT and HOT-lane observations stay.
                _open_paper = canonical_id in keep or bool(keep & aliases)
                fixture = record.lifecycle_fixture()
                lifecycle_hot = False
                if fixture is not None:
                    lifecycle_hot = (
                        classify_scan_lane(fixture, evaluated, **classify_kwargs) is ScanLane.HOT
                    )
                if record.hot is not None:
                    record.universe = None
                    if record.markets:
                        record.markets = {
                            key: slot
                            for key, slot in record.markets.items()
                            if slot.scan_lane is not ScanLane.UNIVERSE
                        }
                    continue
                if lifecycle_hot or _open_paper:
                    continue
                self._drop_identity(canonical_id)
            # Clear / Clear & update drops the UNIVERSE catalogue. HOT rows that
            # remain are not catalogue events until UNIVERSE publishes them again.
            self._universe_catalogue.clear(seen_at=evaluated)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def reset_generation(self) -> int:
        """Monotonic reset epoch. Pre-reset projections must not commit after clear()."""

        with self._lock:
            return self._reset_generation

    def replace_from_report(self, report: CollectionReport) -> None:
        """Compatibility generation replace used by explicit diagnostic collects."""

        self.clear(keep_tombstones=True, keep_universe_generation=True)
        self.upsert_from_report(
            report,
            scan_lane=ScanLane.UNIVERSE,
            publish_universe_catalogue=True,
        )

    def open_universe_generation(self, generation_id: int, *, started_at: datetime | None = None) -> None:
        with self._lock:
            incoming = int(generation_id)
            previous = self._open_universe_generation_id
            if previous is not None and previous != incoming:
                self._record_generation_close_unlocked(previous, started_at)
            self._open_universe_generation_id = incoming

    def close_universe_generation(self, generation_id: int, *, closed_at: datetime) -> None:
        with self._lock:
            proving = int(generation_id)
            self._record_generation_close_unlocked(proving, closed_at)
            if self._open_universe_generation_id == proving:
                self._open_universe_generation_id = None

    def _record_generation_close_unlocked(
        self, generation_id: int, closed_at: datetime | None
    ) -> None:
        if closed_at is None:
            return
        instant = require_aware_instant(closed_at, "closed_at")
        recorded = self._universe_generation_closed_at_by_id.get(generation_id)
        if recorded is None:
            self._universe_generation_closed_at_by_id[generation_id] = instant
            recorded = instant
        self._stamp_generation_close_unlocked(generation_id, recorded)

    def _stamp_generation_close_unlocked(self, generation_id: int, closed_at: datetime) -> None:
        for record in self._rows.values():
            if not record.markets:
                continue
            updated: dict[str, CurrentMarketSlot] = {}
            changed = False
            for key, slot in record.markets.items():
                if (
                    slot.universe_generation_id == generation_id
                    and slot.universe_generation_closed_at is None
                ):
                    updated[key] = replace(slot, universe_generation_closed_at=closed_at)
                    changed = True
                else:
                    updated[key] = slot
            if changed:
                record.markets = updated

    def upsert_from_report(
        self,
        report: CollectionReport,
        *,
        scan_lane: ScanLane | str = ScanLane.UNIVERSE,
        now: datetime | None = None,
        universe_generation_id: int | None = None,
        reset_generation: int | None = None,
        publish_universe_catalogue: bool = False,
        pricing_refresh: bool = False,
    ) -> None:
        with self._lock:
            if (
                reset_generation is not None
                and reset_generation != self._reset_generation
            ):
                return
            self._upsert_from_report_unlocked(
                report,
                scan_lane=scan_lane,
                now=now,
                universe_generation_id=universe_generation_id,
                publish_universe_catalogue=publish_universe_catalogue,
                pricing_refresh=pricing_refresh,
            )

    def upsert_evaluated_fixture(
        self,
        fixture: DiscoveredFixture,
        *,
        markets: list[Any] | None = None,
        decisions: list[Any] | None = None,
        aliases: dict[str, str] | None = None,
        source_events: list[dict[str, Any]] | None = None,
        scan_lane: ScanLane | str = ScanLane.UNIVERSE,
        now: datetime | None = None,
        universe_generation_id: int | None = None,
        publish_universe_catalogue: bool = False,
    ) -> None:
        """Stream one evaluated fixture into current state immediately."""

        scanned = now or fixture.last_scanned_at or fixture.last_seen_at
        canonical_id = fixture.canonical_event_id
        report = CollectionReport(
            started_at=scanned,
            completed_at=scanned,
            discovered_fixtures=[fixture],
            fixture_markets={canonical_id: list(markets or [])},
            paper_decisions=list(decisions or []),
            scan_lane=ScanLane(scan_lane).value if not isinstance(scan_lane, str) else scan_lane,
            fixture_identity_aliases=dict(aliases or {canonical_id: canonical_id}),
            fixture_source_events={canonical_id: list(source_events or [])},
        )
        self.upsert_from_report(
            report,
            scan_lane=scan_lane,
            now=scanned,
            universe_generation_id=universe_generation_id,
            publish_universe_catalogue=publish_universe_catalogue,
        )

    def _upsert_from_report_unlocked(
        self,
        report: CollectionReport,
        *,
        scan_lane: ScanLane | str = ScanLane.UNIVERSE,
        now: datetime | None = None,
        universe_generation_id: int | None = None,
        publish_universe_catalogue: bool = False,
        pricing_refresh: bool = False,
    ) -> None:
        lane = ScanLane(scan_lane) if not isinstance(scan_lane, ScanLane) else scan_lane
        if lane is ScanLane.DROP:
            lane = ScanLane.UNIVERSE
        scanned_at = require_aware_instant(now or report.completed_at, "last_scanned_at")
        stamped_generation_id = self._resolve_upsert_generation_id(
            lane, universe_generation_id, report, pricing_refresh=pricing_refresh
        )
        fixtures = {
            fixture.canonical_event_id: fixture for fixture in report.discovered_fixtures
        }
        markets = {
            event_id: list(rows) for event_id, rows in report.fixture_markets.items()
        }
        paper_ids = _paper_market_ids_by_fixture(report)
        source_events = _source_events_from_report(report, fixtures)
        incoming_aliases = _aliases_from_report(report, fixtures)
        aliases_by_canonical = _aliases_by_canonical(incoming_aliases)
        merge_map: dict[str, str] = {}
        for canonical_id, fixture in fixtures.items():
            aliases = aliases_by_canonical.get(canonical_id, set()) | {canonical_id}
            if self._reject_or_tombstone_incoming(
                canonical_id,
                fixture,
                aliases=aliases,
                scanned_at=scanned_at,
            ):
                if publish_universe_catalogue and lane is ScanLane.UNIVERSE:
                    self._universe_catalogue.remove(canonical_id, seen_at=scanned_at)
                continue
            target_id = self._merge_target_identity(canonical_id, aliases)
            target_id = self._merge_scheduling_identity(target_id, fixture)
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
                if publish_universe_catalogue and lane is ScanLane.UNIVERSE:
                    self._universe_catalogue.remove(target_id, seen_at=scanned_at)
                    if target_id != canonical_id:
                        self._universe_catalogue.remove(canonical_id, seen_at=scanned_at)
                continue
            if authoritative_post_kickoff_zero_equivalents(fixture, scanned_at):
                self._record_market_closure(
                    target_id,
                    fixture,
                    aliases=aliases | {canonical_id, target_id},
                    scanned_at=scanned_at,
                )
                if target_id != canonical_id:
                    self._drop_identity(canonical_id)
                continue
            evaluated = successful_complete_market_evaluation(fixture)
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
                universe_generation_id=stamped_generation_id,
                pricing_refresh=pricing_refresh,
            )
            self._upsert_observation(target_id, observation)
            if publish_universe_catalogue and lane is ScanLane.UNIVERSE and not pricing_refresh:
                if target_id != canonical_id:
                    self._universe_catalogue.remove(canonical_id, seen_at=scanned_at)
                self._universe_catalogue.upsert(
                    _universe_catalogue_fixture(
                        stored_fixture,
                        generation_id=stamped_generation_id,
                        updated_at=scanned_at,
                    ),
                    seen_at=scanned_at,
                )
            self._bind_aliases(aliases | {canonical_id, target_id}, target_id)
            for event in observation.source_events:
                self._bind_alias(event.source_event_id, target_id)
            self._bind_scheduling_key(stored_fixture, target_id)
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
            kwargs = self._store_market_kwargs(kwargs)
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

    def _store_market_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        merged = dict(kwargs)
        if "open_universe_generation_id" not in merged:
            merged["open_universe_generation_id"] = self._open_universe_generation_id
        if "universe_generation_closed_at_by_id" not in merged:
            merged["universe_generation_closed_at_by_id"] = dict(
                self._universe_generation_closed_at_by_id
            )
        band, depth = self._hot_proximity_limits
        if "hot_proximity_band_pp" not in merged:
            merged["hot_proximity_band_pp"] = band
        if "hot_minimum_limiting_depth_gbp" not in merged:
            merged["hot_minimum_limiting_depth_gbp"] = depth
        return merged

    def _resolve_upsert_generation_id(
        self,
        lane: ScanLane,
        explicit: int | None,
        report: CollectionReport,
        *,
        pricing_refresh: bool = False,
    ) -> int | None:
        if pricing_refresh or lane is not ScanLane.UNIVERSE:
            return None
        if explicit is not None:
            return int(explicit)
        if self._open_universe_generation_id is not None:
            return self._open_universe_generation_id
        diagnostics = getattr(report, "scan_diagnostics", None) or {}
        raw = diagnostics.get("universe_generation_id")
        if raw in (None, ""):
            return None
        return int(raw)

    def inventory(
        self,
        now: datetime,
        *,
        hot_horizon=DEFAULT_HOT_HORIZON,
        post_kickoff_unknown_horizon=DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
        post_kickoff_current_radar_ceiling=DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
        hot_interval_seconds: int = DEFAULT_HOT_INTERVAL_SECONDS,
        universe_interval_seconds: int = DEFAULT_UNIVERSE_INTERVAL_SECONDS,
        hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
        background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
        max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    ) -> list[DiscoveredFixture]:
        """Projected fixture board. Evicts under the store lock, projects outside it."""

        return self.operator_board(
            now,
            hot_horizon=hot_horizon,
            post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
            post_kickoff_current_radar_ceiling=post_kickoff_current_radar_ceiling,
            hot_interval_seconds=hot_interval_seconds,
            universe_interval_seconds=universe_interval_seconds,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            background_current_state_ttl_seconds=background_current_state_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        ).discovered

    def current_radar_rows(
        self,
        now: datetime,
        *,
        hot_horizon=DEFAULT_HOT_HORIZON,
        post_kickoff_unknown_horizon=DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
        post_kickoff_current_radar_ceiling=DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
        hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
        background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
        hot_interval_seconds: int = DEFAULT_HOT_INTERVAL_SECONDS,
        universe_interval_seconds: int = DEFAULT_UNIVERSE_INTERVAL_SECONDS,
        quote_age_ms_by_market: dict[str, int | None] | None = None,
        max_quote_age_ms: int = 1000,
        **kwargs: Any,
    ) -> list[FixtureRadarRow]:
        with self._lock:
            return self._current_radar_rows_unlocked(
                now,
                hot_horizon=hot_horizon,
                post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
                post_kickoff_current_radar_ceiling=post_kickoff_current_radar_ceiling,
                hot_ttl_seconds=hot_ttl_seconds,
                universe_ttl_seconds=universe_ttl_seconds,
                background_current_state_ttl_seconds=background_current_state_ttl_seconds,
                hot_interval_seconds=hot_interval_seconds,
                universe_interval_seconds=universe_interval_seconds,
                quote_age_ms_by_market=quote_age_ms_by_market,
                max_quote_age_ms=max_quote_age_ms,
                **kwargs,
            )

    def _current_radar_rows_unlocked(
        self,
        now: datetime,
        *,
        hot_horizon=DEFAULT_HOT_HORIZON,
        post_kickoff_unknown_horizon=DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
        post_kickoff_current_radar_ceiling=DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
        hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
        background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
        hot_interval_seconds: int = DEFAULT_HOT_INTERVAL_SECONDS,
        universe_interval_seconds: int = DEFAULT_UNIVERSE_INTERVAL_SECONDS,
        quote_age_ms_by_market: dict[str, int | None] | None = None,
        max_quote_age_ms: int = 1000,
        **kwargs: Any,
    ) -> list[FixtureRadarRow]:
        evaluated = require_aware_instant(now, "now")
        quote_ages = quote_age_ms_by_market or {}
        current: list[FixtureRadarRow] = []
        market_kwargs = self._store_market_kwargs(
            {
                "hot_ttl_seconds": hot_ttl_seconds,
                "universe_ttl_seconds": universe_ttl_seconds,
                "background_current_state_ttl_seconds": background_current_state_ttl_seconds,
                "max_quote_age_ms": max_quote_age_ms,
                **kwargs,
            }
        )
        self._evict_non_current(
            evaluated,
            hot_horizon=hot_horizon,
            post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
            post_kickoff_current_radar_ceiling=post_kickoff_current_radar_ceiling,
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
                    "post_kickoff_current_radar_ceiling": post_kickoff_current_radar_ceiling,
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

    def market_price_clock(
        self,
        canonical_market_id: str,
        now: datetime,
        *,
        source_market_ids: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> MarketPriceClock:
        """Market-specific price and discovery clocks already stored in current state.

        Does not call providers. An ambiguous slot match stays unpriced rather
        than borrowing another market's clock or the fixture discovery time.
        """

        wanted = canonical_market_id.strip()
        if not wanted:
            return MarketPriceClock(None, None, None)
        sources = {item.strip() for item in source_market_ids if item and str(item).strip()}
        with self._lock:
            evaluated = require_aware_instant(now, "now")
            market_kwargs = self._store_market_kwargs(kwargs)
            for record in self._rows.values():
                record.prune_markets(evaluated, **market_kwargs)
                slots = record.live_market_slots()
                if not _record_mentions_market(record, slots, wanted):
                    continue
                discovered = (
                    record.universe.last_scanned_at
                    if record.universe is not None and record.universe.evaluated
                    else None
                )
                slot = _select_market_price_slot(slots, wanted, sources)
                return _clock_for_slot(discovered, slot, record, sources)
        return MarketPriceClock(None, None, None)

    def hot_identity_scope(self, now: datetime, **kwargs: Any) -> list[str]:
        with self._lock:
            evaluated = require_aware_instant(now, "now")
            kwargs = self._store_market_kwargs(kwargs)
            classify_kwargs = _classify_kwargs(kwargs)
            market_kwargs = _market_ttl_kwargs(kwargs)
            self._evict_non_current(evaluated, **classify_kwargs, **market_kwargs)
            fixtures: list[DiscoveredFixture] = []
            for record in list(self._rows.values()):
                lifecycle = record.lifecycle_fixture()
                if lifecycle is None:
                    continue
                membership = self._identity_membership(
                    record,
                    lifecycle,
                    evaluated,
                    classify_kwargs=classify_kwargs,
                    market_kwargs=market_kwargs,
                )
                if membership is ScanLane.HOT:
                    fixture = record.status_fixture(evaluated, **market_kwargs) or lifecycle
                    if current_slots_prove_qualifying_opportunity(
                        record.live_market_slots(),
                        now=evaluated,
                        **market_kwargs,
                    ):
                        fixture = fixture.model_copy(update={"solver_is_arbitrage": True})
                    fixtures.append(fixture)
            fixtures.sort(key=hot_sort_key)
            return self._unique_hot_canonical_ids(fixtures)

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

    def hot_market_relationships(
        self,
        canonical_ids: list[str],
        now: datetime | None = None,
        **kwargs: Any,
    ) -> dict[str, list[HotMarketRelationship]]:
        """Current non-absent MATCHED_EQUIVALENT identities HOT may quote-refresh.

        Prices on inventory rows are not relationship identity. Generation-aware
        ApprovedEquivalent presence still gates which rows are current.
        """

        with self._lock:
            kwargs = self._store_market_kwargs(kwargs)
            market_kwargs = _market_ttl_kwargs(kwargs)
            evaluated = require_aware_instant(now or datetime.now(UTC), "now")
            payload: dict[str, list[HotMarketRelationship]] = {}
            for canonical_id in canonical_ids:
                record = self._rows.get(canonical_id)
                if record is None:
                    continue
                record.prune_markets(evaluated, **market_kwargs)
                items = relationships_from_current_slots(
                    canonical_id,
                    record.live_market_slots(),
                    now=evaluated,
                    **market_kwargs,
                )
                if items:
                    payload[canonical_id] = items
            return payload

    def upsert_evaluated_fixture_counting_hot(
        self,
        fixture: DiscoveredFixture,
        *,
        now: datetime,
        **upsert_kwargs: Any,
    ) -> tuple[int, int]:
        """``upsert_evaluated_fixture`` bracketed by exact unique-HOT unit counts.

        Same result as ``membership_counts(now)[0]`` immediately before and after
        the upsert. At one instant an upsert can only change the records it
        touches, so the after-count reclassifies those and reuses the before pass
        (in store order) for every other record.
        """

        with self._lock:
            evaluated = require_aware_instant(now, "now")
            kwargs = self._store_market_kwargs({})
            classify_kwargs = _classify_kwargs(kwargs)
            market_kwargs = _market_ttl_kwargs(kwargs)
            self._evict_non_current(evaluated, **classify_kwargs, **market_kwargs)
            before = {
                canonical_id: self._hot_membership_fixture(
                    record, evaluated, classify_kwargs, market_kwargs
                )
                for canonical_id, record in list(self._rows.items())
            }
            before_hot = len(
                unique_hot_scheduling_ids([item for item in before.values() if item is not None])
            )
            self._touched_ids = set()
            try:
                self.upsert_evaluated_fixture(fixture, now=now, **upsert_kwargs)
            finally:
                touched, self._touched_ids = self._touched_ids, None
            changed = {
                canonical_id
                for canonical_id in self._rows
                if canonical_id in touched or canonical_id not in before
            }
            for canonical_id, record in list(self._rows.items()):
                if canonical_id in changed:
                    self._evict_record(
                        canonical_id, record, evaluated, classify_kwargs, market_kwargs
                    )
            after: list[DiscoveredFixture] = []
            for canonical_id, record in list(self._rows.items()):
                item = (
                    self._hot_membership_fixture(
                        record, evaluated, classify_kwargs, market_kwargs
                    )
                    if canonical_id in changed
                    else before[canonical_id]
                )
                if item is not None:
                    after.append(item)
            return before_hot, len(unique_hot_scheduling_ids(after))

    def _hot_membership_fixture(
        self,
        record: _FixtureRecord,
        now: datetime,
        classify_kwargs: dict[str, Any],
        market_kwargs: dict[str, Any],
    ) -> DiscoveredFixture | None:
        lifecycle = record.lifecycle_fixture()
        if lifecycle is None:
            return None
        membership = self._identity_membership(
            record,
            lifecycle,
            now,
            classify_kwargs=classify_kwargs,
            market_kwargs=market_kwargs,
        )
        if membership is not ScanLane.HOT:
            return None
        return record.status_fixture(now, **market_kwargs) or lifecycle

    def universe_catalogue_metadata(self) -> UniverseCatalogueMetadata:
        """Count and revision only. Does not sort or copy fixture rows."""

        with self._lock:
            return self._universe_catalogue.metadata()

    def universe_catalogue_snapshot(self) -> UniverseCatalogueSnapshot:
        with self._lock:
            return self._universe_catalogue.snapshot()

    def console_projection(self, now: datetime, **kwargs: Any) -> _ConsoleProjection:
        """HOT roster and counts without projecting every discovered fixture.

        Eviction still runs, matching ``operator_board``. Full fixture economics
        run only for the small non-deferred HOT set.
        """

        views, classify_kwargs, market_kwargs = self._capture_status_views(now, **kwargs)
        hot_horizon = kwargs.get("hot_horizon", DEFAULT_HOT_HORIZON)
        hot_for_counts: list[DiscoveredFixture] = []
        hot_roster: list[HotRosterEntry] = []
        universe = 0
        lifecycle_hot = 0
        promoted = 0
        deferred = 0
        for record in views:
            lifecycle = record.lifecycle_fixture()
            if lifecycle is None:
                continue
            membership = self._identity_membership(
                record,
                lifecycle,
                now,
                classify_kwargs=classify_kwargs,
                market_kwargs=market_kwargs,
            )
            if membership is ScanLane.DROP:
                continue
            if str(lifecycle.market_evaluation_state or "") in AWAITING_CROSS_VENUE_STATES:
                deferred += 1
            if membership is ScanLane.HOT:
                hot_for_counts.append(lifecycle)
                classified = classify_scan_lane(lifecycle, now, **classify_kwargs)
                if classified is ScanLane.HOT:
                    lifecycle_hot += 1
                else:
                    promoted += 1
                if str(lifecycle.market_evaluation_state or "") in AWAITING_CROSS_VENUE_STATES:
                    continue
                projected = record.status_fixture(now, **market_kwargs) or lifecycle
                lane, scanned = record.scheduler_lane_scan(membership, projected)
                qualifying_promotion = False
                surveillance_promotion = False
                net_proximity_promotion = False
                execution_miss_promotion = False
                net_proximity_distance_pp = None
                if classified is ScanLane.UNIVERSE:
                    qualifying_promotion = current_slots_prove_qualifying_opportunity(
                        record.live_market_slots(),
                        now=now,
                        **market_kwargs,
                    )
                    net_proximity_distance_pp = current_slots_net_proximity_distance_pp(
                        record.live_market_slots(),
                        now=now,
                        **market_kwargs,
                    )
                    net_proximity_promotion = (
                        not qualifying_promotion and net_proximity_distance_pp is not None
                    )
                    surveillance_promotion = (
                        not qualifying_promotion
                        and not net_proximity_promotion
                        and current_slots_prove_surveillance_opportunity(
                            record.live_market_slots(),
                            now=now,
                            **market_kwargs,
                        )
                    )
                    execution_miss_promotion = (
                        not qualifying_promotion
                        and not net_proximity_promotion
                        and not surveillance_promotion
                        and self._execution_miss_hot_active(projected.canonical_event_id, now)
                    )
                stamped = projected.model_copy(
                    update={
                        "scan_lane": lane.value,
                        "last_scanned_at": scanned,
                        "hot_reasons": hot_reason_labels(
                            projected,
                            now,
                            membership=membership,
                            lifecycle=classified,
                            qualifying_promotion=qualifying_promotion,
                            surveillance_promotion=surveillance_promotion,
                            net_proximity_promotion=net_proximity_promotion,
                            net_proximity_distance_pp=net_proximity_distance_pp,
                            execution_miss_promotion=execution_miss_promotion,
                            hot_horizon=hot_horizon,
                        ),
                    }
                )
                hot_roster.append(_hot_roster_entry(stamped))
            elif membership is ScanLane.UNIVERSE:
                universe += 1
        hot_roster.sort(key=lambda item: (item.kickoff_utc, item.canonical_event_id))
        return _ConsoleProjection(
            as_of=require_aware_instant(now, "now"),
            hot_count=len(unique_hot_scheduling_ids(hot_for_counts)),
            universe_count=universe,
            breakdown=(
                len(unique_hot_scheduling_ids(hot_for_counts)),
                lifecycle_hot,
                promoted,
            ),
            hot_roster=hot_roster,
            deferred_awaiting_count=deferred,
        )

    def deferred_fixture_report(self, now: datetime, **kwargs: Any) -> DeferredFixtureReport:
        """Detailed deferred rows. Reads retained state. Does not scan or call venues."""

        views, classify_kwargs, market_kwargs = self._capture_status_views(now, **kwargs)
        rows: list[DeferredFixtureRow] = []
        for record in views:
            lifecycle = record.lifecycle_fixture()
            if lifecycle is None:
                continue
            membership = self._identity_membership(
                record,
                lifecycle,
                now,
                classify_kwargs=classify_kwargs,
                market_kwargs=market_kwargs,
            )
            if membership is ScanLane.DROP:
                continue
            if str(lifecycle.market_evaluation_state or "") not in AWAITING_CROSS_VENUE_STATES:
                continue
            rows.append(_deferred_row(lifecycle))
        rows.sort(key=lambda item: (item.kickoff_utc, item.canonical_event_id))
        return DeferredFixtureReport(count=len(rows), rows=rows)

    def operator_board(
        self,
        now: datetime,
        *,
        hot_horizon=DEFAULT_HOT_HORIZON,
        post_kickoff_unknown_horizon=DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
        post_kickoff_current_radar_ceiling=DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
        hot_interval_seconds: int = DEFAULT_HOT_INTERVAL_SECONDS,
        universe_interval_seconds: int = DEFAULT_UNIVERSE_INTERVAL_SECONDS,
        hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
        background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
        max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    ) -> OperatorBoard:
        """One eviction, then inventory and HOT counts projected off the store lock.

        ``as_of`` is the classification instant. A fixture published while the
        projection runs appears on the next read; this board does not pretend
        to be newer than ``as_of``.
        """

        views, classify_kwargs, market_kwargs = self._capture_status_views(
            now,
            hot_horizon=hot_horizon,
            post_kickoff_unknown_horizon=post_kickoff_unknown_horizon,
            post_kickoff_current_radar_ceiling=post_kickoff_current_radar_ceiling,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            background_current_state_ttl_seconds=background_current_state_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        )
        return OperatorBoard(
            as_of=require_aware_instant(now, "now"),
            discovered=self._project_inventory(
                views,
                now,
                classify_kwargs=classify_kwargs,
                market_kwargs=market_kwargs,
                hot_horizon=hot_horizon,
                hot_interval_seconds=hot_interval_seconds,
                universe_interval_seconds=universe_interval_seconds,
            ),
            membership=self._project_membership(
                views, now, classify_kwargs=classify_kwargs, market_kwargs=market_kwargs
            ),
            breakdown=self._project_hot_breakdown(
                views, now, classify_kwargs=classify_kwargs, market_kwargs=market_kwargs
            ),
        )

    def _capture_status_views(
        self, now: datetime, **kwargs: Any
    ) -> tuple[list[_StatusView], dict[str, Any], dict[str, Any]]:
        """Evict and copy fixture inputs. Callers project after this returns."""

        with self._lock:
            stored = self._store_market_kwargs(kwargs)
            classify_kwargs = _classify_kwargs(stored)
            market_kwargs = _market_ttl_kwargs(stored)
            self._evict_non_current(now, **classify_kwargs, **market_kwargs)
            views = [_StatusView(record) for record in list(self._rows.values())]
        return views, classify_kwargs, market_kwargs

    def _project_inventory(
        self,
        views: list[_StatusView],
        now: datetime,
        *,
        classify_kwargs: dict[str, Any],
        market_kwargs: dict[str, Any],
        hot_horizon,
        hot_interval_seconds: int,
        universe_interval_seconds: int,
    ) -> list[DiscoveredFixture]:
        rows: list[DiscoveredFixture] = []
        for record in views:
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
            if membership is ScanLane.DROP:
                continue
            lane, scanned = record.scheduler_lane_scan(membership, fixture)
            lifecycle = classify_scan_lane(fixture, now, **classify_kwargs)
            qualifying_promotion = False
            surveillance_promotion = False
            net_proximity_promotion = False
            execution_miss_promotion = False
            net_proximity_distance_pp = None
            if membership is ScanLane.HOT and lifecycle is ScanLane.UNIVERSE:
                qualifying_promotion = current_slots_prove_qualifying_opportunity(
                    record.live_market_slots(),
                    now=now,
                    **market_kwargs,
                )
                net_proximity_distance_pp = current_slots_net_proximity_distance_pp(
                    record.live_market_slots(),
                    now=now,
                    **market_kwargs,
                )
                net_proximity_promotion = (
                    not qualifying_promotion and net_proximity_distance_pp is not None
                )
                surveillance_promotion = (
                    not qualifying_promotion
                    and not net_proximity_promotion
                    and current_slots_prove_surveillance_opportunity(
                        record.live_market_slots(),
                        now=now,
                        **market_kwargs,
                    )
                )
                execution_miss_promotion = (
                    not qualifying_promotion
                    and not net_proximity_promotion
                    and not surveillance_promotion
                    and self._execution_miss_hot_active(fixture.canonical_event_id, now)
                )
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
                        "hot_reasons": hot_reason_labels(
                            fixture,
                            now,
                            membership=membership,
                            lifecycle=lifecycle,
                            qualifying_promotion=qualifying_promotion,
                            surveillance_promotion=surveillance_promotion,
                            net_proximity_promotion=net_proximity_promotion,
                            net_proximity_distance_pp=net_proximity_distance_pp,
                            execution_miss_promotion=execution_miss_promotion,
                            hot_horizon=hot_horizon,
                        ),
                    }
                )
            )
        rows.sort(key=lambda item: (item.kickoff_utc, item.canonical_event_id))
        return rows

    def _project_membership(
        self,
        views: list[_StatusView],
        now: datetime,
        *,
        classify_kwargs: dict[str, Any],
        market_kwargs: dict[str, Any],
    ) -> tuple[int, int]:
        hot_fixtures: list[DiscoveredFixture] = []
        universe = 0
        for record in views:
            lifecycle = record.lifecycle_fixture()
            if lifecycle is None:
                continue
            membership = self._identity_membership(
                record,
                lifecycle,
                now,
                classify_kwargs=classify_kwargs,
                market_kwargs=market_kwargs,
            )
            if membership is ScanLane.HOT:
                hot_fixtures.append(record.status_fixture(now, **market_kwargs) or lifecycle)
            elif membership is ScanLane.UNIVERSE:
                universe += 1
        return len(unique_hot_scheduling_ids(hot_fixtures)), universe

    def _project_hot_breakdown(
        self,
        views: list[_StatusView],
        now: datetime,
        *,
        classify_kwargs: dict[str, Any],
        market_kwargs: dict[str, Any],
    ) -> tuple[int, int, int]:
        hot_fixtures: list[DiscoveredFixture] = []
        lifecycle = 0
        promoted = 0
        for record in views:
            lifecycle_fixture = record.lifecycle_fixture()
            if lifecycle_fixture is None:
                continue
            membership = self._identity_membership(
                record,
                lifecycle_fixture,
                now,
                classify_kwargs=classify_kwargs,
                market_kwargs=market_kwargs,
            )
            if membership is not ScanLane.HOT:
                continue
            hot_fixtures.append(record.status_fixture(now, **market_kwargs) or lifecycle_fixture)
            classified = classify_scan_lane(lifecycle_fixture, now, **classify_kwargs)
            if classified is ScanLane.HOT:
                lifecycle += 1
            else:
                promoted += 1
        return len(unique_hot_scheduling_ids(hot_fixtures)), lifecycle, promoted

    def membership_counts(self, now: datetime, **kwargs: Any) -> tuple[int, int]:
        views, classify_kwargs, market_kwargs = self._capture_status_views(now, **kwargs)
        return self._project_membership(
            views, now, classify_kwargs=classify_kwargs, market_kwargs=market_kwargs
        )

    def hot_membership_breakdown(self, now: datetime, **kwargs: Any) -> tuple[int, int, int]:
        """Return (unique HOT units, lifecycle HOT, promoted HOT)."""

        views, classify_kwargs, market_kwargs = self._capture_status_views(now, **kwargs)
        return self._project_hot_breakdown(
            views, now, classify_kwargs=classify_kwargs, market_kwargs=market_kwargs
        )

    def _identity_membership(
        self,
        record: _FixtureRecord,
        fixture: DiscoveredFixture,
        now: datetime,
        *,
        classify_kwargs: dict[str, Any],
        market_kwargs: dict[str, Any],
    ) -> ScanLane:
        """HOT identity = lifecycle HOT or current surveillance/qualifying promotion.

        `classify_scan_lane` remains the lifecycle classifier. Promotion reads
        merged current-state market truth, not UI labels or historical audit.
        Already-triggered net ROI or 0.50pp net proximity is enough to watch;
        paper entry stays behind the existing executable/allocator gates and
        the post-trigger Min Net Arb arrival check.
        """

        lifecycle = classify_scan_lane(fixture, now, **classify_kwargs)
        if lifecycle is ScanLane.DROP or lifecycle is ScanLane.HOT:
            return lifecycle
        if self._execution_miss_hot_active(fixture.canonical_event_id, now):
            return ScanLane.HOT
        slots = record.live_market_slots()
        if current_slots_prove_qualifying_opportunity(slots, now=now, **market_kwargs):
            return ScanLane.HOT
        if current_slots_prove_surveillance_opportunity(slots, now=now, **market_kwargs):
            return ScanLane.HOT
        return lifecycle

    def note_execution_miss_hot(self, canonical_event_id: str, *, until: datetime) -> None:
        """Remember a zero-fill execution-miss HOT window. Not a durable queue."""

        with self._lock:
            self._execution_miss_hot_until[str(canonical_event_id)] = until

    def clear_execution_miss_hot(self, canonical_event_id: str | None = None) -> None:
        with self._lock:
            if canonical_event_id is None:
                self._execution_miss_hot_until.clear()
                return
            self._execution_miss_hot_until.pop(str(canonical_event_id), None)

    def _execution_miss_hot_active(self, canonical_event_id: str, now: datetime) -> bool:
        with self._lock:
            until = self._execution_miss_hot_until.get(str(canonical_event_id))
            if until is None:
                return False
            evaluated = require_aware_instant(now, "now")
            if evaluated < until:
                return True
            self._execution_miss_hot_until.pop(str(canonical_event_id), None)
            return False

    def _merge_scheduling_identity(self, target_id: str, fixture: Any) -> str:
        """Record a diagnostic HOT scheduling token. Never absorbs canonical identity.

        Unique HOT units use the inclusive 5-minute relation in
        ``unique_hot_scheduling_ids``, not this index. Trusted source/canonical
        overlap still merges via `_merge_target_identity`. A team+kickoff
        collision without that evidence must not blend records.
        """

        key = hot_scheduling_key(fixture)
        if key is None:
            return target_id
        existing = self._scheduling_index.get(key)
        if existing is None or existing not in self._rows:
            self._scheduling_index[key] = target_id
        return target_id

    def _bind_scheduling_key(self, fixture: Any, target_id: str) -> None:
        key = hot_scheduling_key(fixture)
        if key is None or target_id not in self._rows:
            return
        existing = self._scheduling_index.get(key)
        if existing is None or existing not in self._rows:
            self._scheduling_index[key] = target_id
            return
        # Keep both canonical rows. Prefer the already-indexed unit for HOT.
        if existing != target_id:
            return
        self._scheduling_index[key] = target_id

    def _unique_hot_canonical_ids(self, fixtures: list[DiscoveredFixture]) -> list[str]:
        """One representative canonical id per inclusive 5-minute HOT unit."""

        return unique_hot_scheduling_ids(fixtures)

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
        self._note_touched(source_id, target_id)
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
        for key, dest in list(self._scheduling_index.items()):
            if dest == source_id:
                self._scheduling_index[key] = target_id

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

    def _note_touched(self, *canonical_ids: str) -> None:
        if self._touched_ids is not None:
            self._touched_ids.update(canonical_ids)

    def _upsert_observation(self, canonical_id: str, observation: LaneObservation) -> None:
        self._note_touched(canonical_id)
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
        if observation.pricing_refresh and observation.scan_lane is not ScanLane.HOT:
            # BACKGROUND pricing refreshes economics only. An existing UNIVERSE
            # discovery observation and its generation provenance stay in place.
            # A first price with no lane observation still installs one, so
            # detail() can classify the fixture instead of evicting an empty lifecycle.
            if record.universe is None and record.hot is None:
                record.set_lane(observation)
            record.merge_markets(observation)
            record.leftover_this_pass = False
            return
        if record.set_lane(observation):
            record.merge_markets(observation)
            record.leftover_this_pass = False

    def _evict_non_current(self, now: datetime, **kwargs: Any) -> None:
        """Drop radar inventory only. Must not mutate paper trades, Treasury, or ACTIVE TRADE."""

        kwargs = self._store_market_kwargs(kwargs)
        classify_kwargs = _classify_kwargs(kwargs)
        market_kwargs = _market_ttl_kwargs(kwargs)
        evaluated = require_aware_instant(now, "now")
        for canonical_id, record in list(self._rows.items()):
            self._evict_record(canonical_id, record, evaluated, classify_kwargs, market_kwargs)

    def _evict_record(
        self,
        canonical_id: str,
        record: _FixtureRecord,
        evaluated: datetime,
        classify_kwargs: dict[str, Any],
        market_kwargs: dict[str, Any],
    ) -> None:
        record.prune_markets(evaluated, **market_kwargs)
        fixture = record.lifecycle_fixture()
        if fixture is None:
            self._drop_identity(canonical_id)
            return
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
                    record.status_fixture(evaluated, **market_kwargs) or fixture,
                    aliases=aliases,
                    scanned_at=evaluated,
                )
            else:
                self._drop_identity(canonical_id)
            return
        if authoritative_post_kickoff_zero_equivalents(fixture, evaluated):
            aliases = {
                alias for alias, target in self._aliases.items() if target == canonical_id
            }
            aliases.add(canonical_id)
            self._record_market_closure(
                canonical_id,
                fixture,
                aliases=aliases,
                scanned_at=evaluated,
            )

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
        if tombstone.reason == EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF and (
            _market_closure_may_restore(fixture, scanned_at)
        ):
            self._clear_tombstone(tombstone.canonical_event_id)
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

    def _record_market_closure(
        self,
        canonical_id: str,
        fixture: DiscoveredFixture,
        *,
        aliases: set[str],
        scanned_at: datetime,
    ) -> None:
        """Leave current radar after a successful zero-equivalent evaluation.

        Does not rewrite ``fixture_status`` or ``in_running``. A later
        successful evaluation that still has equivalents, provider
        ``in_running``, or a pre-kickoff/schedule correction may restore.
        """

        existing = self._tombstones.get(canonical_id)
        if existing is not None and existing.reason != EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF:
            return
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
        tombstone = CurrentStateTombstone(
            canonical_event_id=canonical_id,
            aliases=frozenset(merged),
            reason=EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF,
            provider_status=fixture.fixture_status,
            source=lifecycle_status_source(fixture),
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
        self._note_touched(canonical_id)
        self._rows.pop(canonical_id, None)
        for alias, target in list(self._aliases.items()):
            if target == canonical_id:
                self._aliases.pop(alias, None)
        for key, target in list(self._scheduling_index.items()):
            if target == canonical_id:
                self._scheduling_index.pop(key, None)


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
            universe_generation_id=observation.universe_generation_id,
            pricing_refresh=observation.pricing_refresh,
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
                universe_generation_id=observation.universe_generation_id,
                pricing_refresh=observation.pricing_refresh,
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

    def lifecycle_fixture(self) -> DiscoveredFixture | None:
        """``status_fixture`` without the current-market projection.

        ``apply_current_market_inventory`` rewrites market-derived fields only.
        Identity, status, kickoff and in-running come from the observation
        itself, so lane classification and eviction must not pay for stamping
        every market row of every fixture.
        """

        observation = self.status_observation()
        return None if observation is None else observation.fixture

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


class _StatusView:
    """Fixture inputs copied under the store lock so projection can run outside it.

    Market slots are replaced, not mutated, so the copied dict stays stable if a
    later write publishes a new slot map.
    """

    def __init__(self, record: _FixtureRecord) -> None:
        self.hot = record.hot
        self.universe = record.universe
        self.leftover_this_pass = record.leftover_this_pass
        self.extra_source_events = record.extra_source_events
        self.markets = None if record.markets is None else dict(record.markets)

    status_observation = _FixtureRecord.status_observation
    lifecycle_fixture = _FixtureRecord.lifecycle_fixture
    live_market_slots = _FixtureRecord.live_market_slots
    lane_observation = _FixtureRecord.lane_observation
    selected_observation = _FixtureRecord.selected_observation
    scheduler_lane_scan = _FixtureRecord.scheduler_lane_scan
    status_fixture = _FixtureRecord.status_fixture


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


def _record_mentions_market(
    record: _FixtureRecord,
    slots: list[CurrentMarketSlot],
    market_id: str,
) -> bool:
    if any(market_id in slot.paper_market_ids for slot in slots):
        return True
    for observation in (record.universe, record.hot):
        if observation is not None and market_id in observation.paper_market_ids:
            return True
    return False


def _slot_source_market_ids(slot: CurrentMarketSlot) -> set[str]:
    found: set[str] = set()
    for facts in (slot.row.matchbook, slot.row.polymarket, slot.row.kalshi):
        if facts is None:
            continue
        source_id = str(getattr(facts, "source_market_id", "") or "").strip()
        if source_id:
            found.add(source_id)
    return found


def _select_market_price_slot(
    slots: list[CurrentMarketSlot],
    market_id: str,
    sources: set[str],
) -> CurrentMarketSlot | None:
    if sources:
        matched = [slot for slot in slots if sources & _slot_source_market_ids(slot)]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            return _agreed_price_slot(matched)
    exact = [slot for slot in slots if tuple(slot.paper_market_ids) == (market_id,)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return _agreed_price_slot(exact)
    containing = [slot for slot in slots if market_id in slot.paper_market_ids]
    if len(containing) == 1:
        return containing[0]
    return _agreed_price_slot(containing)


def _price_signature(slot: CurrentMarketSlot) -> tuple[datetime | None, ScanLane, datetime]:
    return (slot.background_priced_at, slot.scan_lane, slot.last_scanned_at)


def _agreed_price_slot(slots: list[CurrentMarketSlot]) -> CurrentMarketSlot | None:
    priced = [
        slot
        for slot in slots
        if slot.background_priced_at is not None or slot.scan_lane is ScanLane.HOT
    ]
    if len(priced) == 1:
        return priced[0]
    if priced and len({_price_signature(slot) for slot in priced}) == 1:
        return priced[0]
    return None


def _clock_for_slot(
    discovered_at: datetime | None,
    slot: CurrentMarketSlot | None,
    record: _FixtureRecord,
    sources: set[str],
) -> MarketPriceClock:
    if slot is not None and slot.scan_lane is not ScanLane.HOT and slot.background_priced_at is not None:
        return MarketPriceClock(discovered_at, slot.background_priced_at, "background")
    if slot is not None and slot.scan_lane is ScanLane.HOT:
        return MarketPriceClock(discovered_at, slot.last_scanned_at, "hot")
    hot_priced_at = _hot_observation_price_at(record, sources)
    if hot_priced_at is not None:
        return MarketPriceClock(discovered_at, hot_priced_at, "hot")
    return MarketPriceClock(discovered_at, None, None)


def _hot_observation_price_at(record: _FixtureRecord, sources: set[str]) -> datetime | None:
    """Last HOT observation time for this market's source ids only.

    A later UNIVERSE confirm rewrites the slot lane. The HOT observation still
    holds the price time, but only for markets that observation actually carried.
    """

    hot = record.hot
    if hot is None or not hot.evaluated or not sources:
        return None
    for row in hot.markets:
        row_ids = {
            str(getattr(facts, "source_market_id", "") or "").strip()
            for facts in (
                getattr(row, "matchbook", None),
                getattr(row, "polymarket", None),
                getattr(row, "kalshi", None),
            )
            if facts is not None
        }
        if sources & {item for item in row_ids if item}:
            return hot.last_scanned_at
    return None


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


def _aliases_by_canonical(aliases: dict[str, str]) -> dict[str, set[str]]:
    """Inverse of ``aliases``; one pass instead of a full scan per fixture."""

    inverse: dict[str, set[str]] = {}
    for alias, target in aliases.items():
        inverse.setdefault(target, set()).add(alias)
    return inverse


def _classify_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed = {}
    if "hot_horizon" in kwargs:
        allowed["hot_horizon"] = kwargs["hot_horizon"]
    if "post_kickoff_unknown_horizon" in kwargs:
        allowed["post_kickoff_unknown_horizon"] = kwargs["post_kickoff_unknown_horizon"]
    if "post_kickoff_current_radar_ceiling" in kwargs:
        allowed["post_kickoff_current_radar_ceiling"] = kwargs["post_kickoff_current_radar_ceiling"]
    return allowed


def _market_ttl_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed: dict[str, Any] = {}
    if "hot_ttl_seconds" in kwargs:
        allowed["hot_ttl_seconds"] = kwargs["hot_ttl_seconds"]
    if "universe_ttl_seconds" in kwargs:
        allowed["universe_ttl_seconds"] = kwargs["universe_ttl_seconds"]
    if "background_current_state_ttl_seconds" in kwargs:
        allowed["background_current_state_ttl_seconds"] = kwargs[
            "background_current_state_ttl_seconds"
        ]
    if "max_quote_age_ms" in kwargs:
        allowed["max_quote_age_ms"] = kwargs["max_quote_age_ms"]
    if "open_universe_generation_id" in kwargs:
        allowed["open_universe_generation_id"] = kwargs["open_universe_generation_id"]
    if "universe_generation_closed_at_by_id" in kwargs:
        allowed["universe_generation_closed_at_by_id"] = kwargs[
            "universe_generation_closed_at_by_id"
        ]
    if "hot_proximity_band_pp" in kwargs:
        allowed["hot_proximity_band_pp"] = kwargs["hot_proximity_band_pp"]
    if "hot_minimum_limiting_depth_gbp" in kwargs:
        allowed["hot_minimum_limiting_depth_gbp"] = kwargs["hot_minimum_limiting_depth_gbp"]
    return allowed


def _universe_catalogue_fixture(
    fixture: DiscoveredFixture,
    *,
    generation_id: int | None,
    updated_at: datetime,
) -> UniverseCatalogueFixture:
    return UniverseCatalogueFixture(
        canonical_event_id=fixture.canonical_event_id,
        sport=fixture.sport,
        competition=fixture.competition,
        target_competition_code=fixture.target_competition_code,
        home_team=fixture.home_team,
        away_team=fixture.away_team,
        kickoff_utc=fixture.kickoff_utc,
        matchbook_matched=fixture.matchbook_matched,
        polymarket_matched=fixture.polymarket_matched,
        kalshi_matched=fixture.kalshi_matched,
        fixture_status=fixture.fixture_status,
        universe_generation_id=generation_id,
        updated_at=updated_at,
    )


def _hot_roster_entry(fixture: DiscoveredFixture) -> HotRosterEntry:
    return HotRosterEntry(
        canonical_event_id=fixture.canonical_event_id,
        home_team=fixture.home_team,
        away_team=fixture.away_team,
        competition=fixture.competition,
        sport=fixture.sport,
        target_competition_code=fixture.target_competition_code,
        kickoff_utc=fixture.kickoff_utc,
        in_running=fixture.in_running,
        fixture_status=fixture.fixture_status,
        live_score_supported=fixture.live_score_supported,
        home_score=fixture.home_score,
        away_score=fixture.away_score,
        hot_reasons=list(fixture.hot_reasons or []),
        scan_lane=fixture.scan_lane or "hot",
        market_evaluation_state=fixture.market_evaluation_state,
        market_evaluation_reason=fixture.market_evaluation_reason,
        solver_is_arbitrage=fixture.solver_is_arbitrage,
        matchbook_matched=fixture.matchbook_matched,
        polymarket_matched=fixture.polymarket_matched,
        kalshi_matched=fixture.kalshi_matched,
        last_scanned_at=fixture.last_scanned_at,
        last_seen_at=fixture.last_seen_at,
        matched_equivalent_count=fixture.matched_equivalent_count,
        current_net_edge=fixture.current_net_edge,
    )


def _deferred_row(fixture: DiscoveredFixture) -> DeferredFixtureRow:
    return DeferredFixtureRow(
        canonical_event_id=fixture.canonical_event_id,
        home_team=fixture.home_team,
        away_team=fixture.away_team,
        competition=fixture.competition,
        sport=fixture.sport,
        target_competition_code=fixture.target_competition_code,
        kickoff_utc=fixture.kickoff_utc,
        fixture_status=fixture.fixture_status,
        in_running=fixture.in_running,
        matchbook_matched=fixture.matchbook_matched,
        polymarket_matched=fixture.polymarket_matched,
        kalshi_matched=fixture.kalshi_matched,
        market_evaluation_state=fixture.market_evaluation_state,
        market_evaluation_reason=fixture.market_evaluation_reason,
        hot_reasons=list(fixture.hot_reasons or []),
        scan_lane=fixture.scan_lane,
        last_scanned_at=fixture.last_scanned_at,
        last_seen_at=fixture.last_seen_at,
    )
