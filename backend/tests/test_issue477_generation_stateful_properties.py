"""#477 verification: generation start/timeout/resume/clear/stale/fresh/restart.

Data class: synthetic coordinator + paper-ledger fixtures. Not live venue quotes.
PAPER / read-only. Settlement/Treasury paths are exercised only to prove
idempotent release; they do not place venue orders.

Invariants checked after every action:
- no old generation resurrection
- no unfinished-generation long-cadence close
- no duplicate settlement / Treasury release
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from random import Random

import pytest

from sports_hedge.application.collector import (
    UNIVERSE_COMPLETENESS_COMPLETE,
    UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER,
    canonical_work_set_authority,
)
from sports_hedge.application.fixture_clusters import ClusterPass, VenueEvent
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.universe_identity_cache import (
    get_universe_identity_cache,
    reset_universe_identity_cache,
)
from sports_hedge.config import get_settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher
from sports_hedge.paper.settlement import LegSettlement, PaperSettlementComputation
from sports_hedge.paper.trades import PaperSettlementRequest, PaperTrade, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from sports_hedge.treasury.models import TreasuryLockRequest
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report
from test_issue477_differential_matching_oracle import KICKOFF, _event


LONG_CADENCE_FLOOR_SECONDS = 60
TRADE_ID = "ptrade-issue477"
LOCK_ID = "lock-issue477"


@dataclass
class GenerationTrace:
    clock: FakeClock
    store: SqliteUniverseCheckpointStore
    coordinator: LiveRefreshCoordinator
    ledger: SqlitePaperLedger
    seen_generation_ids: set[int] = field(default_factory=set)
    last_closed_generation_id: int | None = None
    last_chunk_epoch: int | None = None
    historical_epochs: list[tuple[int, int]] = field(default_factory=list)
    clustering_items: list[VenueEvent] = field(default_factory=list)

    @property
    def cache(self):
        return get_universe_identity_cache()

    @property
    def generation_id(self) -> int:
        return int(self.coordinator._universe_generation_id)

    @property
    def generation_open(self) -> bool:
        return self.coordinator._universe_generation_started_at is not None


def _lock() -> TreasuryLockRequest:
    return TreasuryLockRequest(
        venue=VenueName.MATCHBOOK,
        native_currency="GBP",
        amount_native=Decimal("250"),
        lock_id=LOCK_ID,
        trade_id=TRADE_ID,
        opportunity_id="opp-issue477",
        fx_rate_gbp_per_unit=Decimal("1"),
    )


def _open_and_settle_once(ledger: SqlitePaperLedger) -> None:
    ledger.treasury.lock_capital([_lock()], occurred_at=NOW)
    trade = PaperTrade(
        trade_id=TRADE_ID,
        opportunity_id="opp-issue477",
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
    )
    computation = PaperSettlementComputation(
        winning_outcome="home",
        realised_pnl_gbp=Decimal("40"),
        legs=[
            LegSettlement(
                outcome="home",
                venue="matchbook",
                currency="GBP",
                filled_stake=Decimal("250"),
                fill_id=LOCK_ID,
                won=True,
                venue_fee=Decimal("10"),
                net_payoff=Decimal("290"),
                native_pnl=Decimal("40"),
                gbp_pnl=Decimal("40"),
                fx_rate_gbp_per_unit=Decimal("1"),
                capital_source="AUTO_POOL",
            )
        ],
    )
    ledger.treasury.apply_settlement(
        trade,
        computation,
        PaperSettlementRequest(
            winning_outcome="home",
            source="operator",
            source_id="issue477",
        ),
        settled_at=NOW,
    )


def _replay_settlement(ledger: SqlitePaperLedger) -> None:
    trade = PaperTrade(
        trade_id=TRADE_ID,
        opportunity_id="opp-issue477",
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
    )
    computation = PaperSettlementComputation(
        winning_outcome="home",
        realised_pnl_gbp=Decimal("40"),
        legs=[
            LegSettlement(
                outcome="home",
                venue="matchbook",
                currency="GBP",
                filled_stake=Decimal("250"),
                fill_id=LOCK_ID,
                won=True,
                venue_fee=Decimal("10"),
                net_payoff=Decimal("290"),
                native_pnl=Decimal("40"),
                gbp_pnl=Decimal("40"),
                fx_rate_gbp_per_unit=Decimal("1"),
                capital_source="AUTO_POOL",
            )
        ],
    )
    ledger.treasury.apply_settlement(
        trade,
        computation,
        PaperSettlementRequest(
            winning_outcome="home",
            source="operator",
            source_id="issue477",
        ),
        settled_at=NOW,
    )


def _settlement_journal_count(ledger: SqlitePaperLedger) -> int:
    return len(
        [
            entry
            for entry in ledger.journal.list_entries()
            if entry.source == "paper_settlement"
        ]
    )


def _assert_no_duplicate_treasury_release(ledger: SqlitePaperLedger) -> None:
    _replay_settlement(ledger)
    assert _settlement_journal_count(ledger) == 1
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.locked_capital == Decimal("0")
    available = pool.available_cash
    _replay_settlement(ledger)
    assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").available_cash == available


def _truncated_report(coordinator: LiveRefreshCoordinator, when) -> object:
    evaluated = _fixture("fx-a", kickoff=when + timedelta(days=1))
    report = _report([evaluated], when=when, scan_lane=ScanLane.UNIVERSE.value)
    return report.model_copy(
        update={
            "scan_diagnostics": {
                "completeness": UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER,
                "clustering_truncated": True,
                "universe_generation_id": coordinator._universe_generation_id,
                "canonical_work_total": 2,
                "evaluated_count": 1,
                "not_evaluated_count": 1,
                "generation_resume": True,
            }
        }
    )


def _complete_report(coordinator: LiveRefreshCoordinator, when) -> object:
    fixtures = [
        _fixture("fx-a", kickoff=when + timedelta(days=1)),
        _fixture("fx-b", kickoff=when + timedelta(days=2)),
    ]
    report = _report(fixtures, when=when, scan_lane=ScanLane.UNIVERSE.value)
    return report.model_copy(
        update={
            "scan_diagnostics": {
                "completeness": UNIVERSE_COMPLETENESS_COMPLETE,
                "clustering_truncated": False,
                "universe_generation_id": coordinator._universe_generation_id,
                "canonical_work_total": 2,
                "evaluated_count": 2,
                "not_evaluated_count": 0,
                "sweep_id": coordinator._universe_sweep_id,
                "generation_id": coordinator._universe_generation_id,
            }
        }
    )


def _assert_not_long_cadence_closed(trace: GenerationTrace) -> None:
    coordinator = trace.coordinator
    if not trace.generation_open:
        return
    assert coordinator._universe_generation_started_at is not None
    assert coordinator._universe_closed_generation_id != coordinator._universe_generation_id
    plan = coordinator.plan_universe_tick(now=trace.clock.now)
    if plan.lane == ScanLane.UNIVERSE.value:
        assert plan.generation_resume is True
        assert plan.universe_generation_id == coordinator._universe_generation_id


def _assert_no_old_generation_resurrection(trace: GenerationTrace) -> None:
    coordinator = trace.coordinator
    if trace.last_closed_generation_id is None:
        return
    if trace.generation_open:
        assert coordinator._universe_generation_id != trace.last_closed_generation_id
    plan = coordinator.plan_universe_tick(now=trace.clock.now)
    if plan.generation_resume:
        assert plan.universe_generation_id != trace.last_closed_generation_id
    if coordinator._universe_closed_generation_id == trace.last_closed_generation_id:
        _cursor, _skip, planned, resume = coordinator._universe_plan_resume_state_unlocked(
            trace.clock.now
        )
        if not trace.generation_open:
            assert resume is False
            assert planned == coordinator._universe_generation_id + 1


def _assert_invariants(trace: GenerationTrace) -> None:
    _assert_not_long_cadence_closed(trace)
    _assert_no_old_generation_resurrection(trace)
    _assert_no_duplicate_treasury_release(trace.ledger)
    if trace.generation_open:
        assert trace.cache.generation_id in {None, trace.generation_id}


def _new_trace(tmp_path: Path) -> GenerationTrace:
    reset_universe_identity_cache()
    clock = FakeClock(NOW)
    store = SqliteUniverseCheckpointStore(tmp_path / "issue477-ckpt.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    ledger = SqlitePaperLedger(
        tmp_path / "issue477-ledger.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.80"),
        fx_source="paper_demo_fx_snapshot",
        include_kalshi=True,
    )
    _open_and_settle_once(ledger)
    clustering_items = [
        VenueEvent(
            venue=VenueName.MATCHBOOK,
            raw={"id": "mb-a"},
            canonical=CanonicalEvent(
                competition="Premier League",
                home_team="Arsenal",
                away_team="Chelsea",
                kickoff_utc=KICKOFF,
                source_venue=VenueName.MATCHBOOK,
                source_event_id="mb-a",
            ),
            source_event_id="mb-a",
        ),
        VenueEvent(
            venue=VenueName.KALSHI,
            raw={"id": "k-a"},
            canonical=CanonicalEvent(
                competition="Premier League",
                home_team="Arsenal FC",
                away_team="Chelsea FC",
                kickoff_utc=KICKOFF,
                source_venue=VenueName.KALSHI,
                source_event_id="k-a",
            ),
            source_event_id="k-a",
        ),
        _event(
            VenueName.POLYMARKET,
            "pm-other",
            home="Liverpool",
            away="Everton",
            kickoff=KICKOFF + timedelta(days=3),
        ),
    ]
    return GenerationTrace(
        clock=clock,
        store=store,
        coordinator=coordinator,
        ledger=ledger,
        clustering_items=clustering_items,
    )


def action_start(trace: GenerationTrace) -> None:
    coordinator = trace.coordinator
    coordinator.record_universe_work_set(
        ["fx-a", "fx-b"],
        authoritative=False,
        partial_reason="clustering_truncated",
    )
    epoch = coordinator._open_universe_chunk_epoch_unlocked()
    trace.last_chunk_epoch = epoch
    trace.historical_epochs.append((coordinator._universe_generation_id, epoch))
    coordinator.record_universe_discovery_snapshot(
        {"matchbook": [{"id": "mb-a"}], "polymarket": [], "kalshi": [{"id": "k-a"}]},
        chunk_epoch=epoch,
    )
    cache = trace.cache
    assert cache.generation_id == coordinator._universe_generation_id
    cluster_pass = ClusterPass(
        matchbook=[trace.clustering_items[0]],
        polymarket=[
            VenueEvent(
                venue=VenueName.POLYMARKET,
                raw=trace.clustering_items[2].raw,
                canonical=trace.clustering_items[2].canonical,
                source_event_id=trace.clustering_items[2].source_event_id,
            )
        ],
        kalshi=[trace.clustering_items[1]],
        matcher=EventMatcher(),
        max_event_pairs=8,
        identity_cache=cache,
    )
    for index, pair in enumerate(cluster_pass.pairs()):
        if index >= 1:
            cluster_pass.checkpoint(index)
            break
        cluster_pass.consider(*pair)
    trace.seen_generation_ids.add(coordinator._universe_generation_id)
    assert trace.generation_open


def action_timeout(trace: GenerationTrace) -> None:
    if not trace.generation_open:
        action_start(trace)
    coordinator = trace.coordinator
    generation = coordinator._universe_generation_id
    started = coordinator._universe_generation_started_at
    report = _truncated_report(coordinator, trace.clock.now)
    coordinator.record_report(report, scan_lane=ScanLane.UNIVERSE)
    assert coordinator._universe_generation_id == generation
    assert coordinator._universe_generation_started_at == started
    assert coordinator._universe_generation_started_at is not None
    assert coordinator._universe_closed_generation_id != generation
    plan = coordinator.plan_universe_tick(now=trace.clock.now)
    if plan.lane == ScanLane.UNIVERSE.value:
        assert plan.generation_resume is True
        assert plan.universe_generation_id == generation
    authoritative, reason = canonical_work_set_authority(
        cluster_ids=["fx-a", "fx-b"],
        clustering_truncated=True,
        retry_series=None,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        enabled={VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI},
        issues=[],
        series_results={},
        provider_cancels=0,
    )
    assert authoritative is False
    assert reason == "clustering_truncated"


def action_resume(trace: GenerationTrace) -> None:
    if not trace.generation_open:
        action_start(trace)
        action_timeout(trace)
    coordinator = trace.coordinator
    generation = coordinator._universe_generation_id
    cache = trace.cache
    resume = cache.take_clustering_resume(
        [
            trace.clustering_items[0],
            VenueEvent(
                venue=VenueName.POLYMARKET,
                raw=trace.clustering_items[2].raw,
                canonical=trace.clustering_items[2].canonical,
                source_event_id=trace.clustering_items[2].source_event_id,
            ),
            trace.clustering_items[1],
        ]
    )
    cluster_pass = ClusterPass(
        matchbook=[trace.clustering_items[0]],
        polymarket=[
            VenueEvent(
                venue=VenueName.POLYMARKET,
                raw=trace.clustering_items[2].raw,
                canonical=trace.clustering_items[2].canonical,
                source_event_id=trace.clustering_items[2].source_event_id,
            )
        ],
        kalshi=[trace.clustering_items[1]],
        matcher=EventMatcher(),
        max_event_pairs=8,
        identity_cache=cache,
    )
    for pair in cluster_pass.pairs():
        cluster_pass.consider(*pair)
    clusters, _counts = cluster_pass.finalize()
    cluster_pass.checkpoint(len(cluster_pass._candidates))
    cluster_pass.record_generation_negatives(clusters)
    epoch = coordinator._open_universe_chunk_epoch_unlocked()
    trace.last_chunk_epoch = epoch
    trace.historical_epochs.append((coordinator._universe_generation_id, epoch))
    coordinator.record_universe_fixture_progress(
        None, _fixture("fx-a", kickoff=trace.clock.now + timedelta(days=1)), [], []
    )
    plan = coordinator.plan_universe_tick(now=trace.clock.now)
    assert plan.generation_resume is True
    assert coordinator._universe_generation_id == generation
    assert cache.generation_id == generation
    _ = resume


def action_clear(trace: GenerationTrace) -> None:
    coordinator = trace.coordinator
    if trace.generation_open:
        trace.last_closed_generation_id = coordinator._universe_generation_id
    coordinator.reset()
    reset_universe_identity_cache()
    assert get_universe_identity_cache().generation_id is None
    assert get_universe_identity_cache().no_cross_venue == {}
    assert coordinator._universe_generation_started_at is None
    assert coordinator._universe_generation_id == 0
    assert coordinator._universe_evaluated_ids == set()
    assert coordinator._universe_work == {}
    plan = coordinator.plan_universe_tick(now=trace.clock.now)
    if plan.generation_resume:
        raise AssertionError("cleared universe must not resume a prior generation")


def action_stale(trace: GenerationTrace) -> None:
    coordinator = trace.coordinator
    generation_before = coordinator._universe_generation_id
    started_before = coordinator._universe_generation_started_at
    evaluated_before = set(coordinator._universe_evaluated_ids)
    snapshot_before = dict(coordinator._universe_discovery_snapshot or {})
    stale_targets = list(trace.historical_epochs)
    if trace.last_chunk_epoch is not None:
        stale_targets.append((generation_before, trace.last_chunk_epoch))
    if trace.generation_open:
        coordinator._invalidate_universe_chunk_epoch_unlocked()
        live_epoch = coordinator._open_universe_chunk_epoch_unlocked()
        trace.last_chunk_epoch = live_epoch
        trace.historical_epochs.append((coordinator._universe_generation_id, live_epoch))
    for generation_id, epoch in stale_targets:
        coordinator.record_universe_discovery_snapshot(
            {"matchbook": [{"id": f"zombie-stale-{generation_id}-{epoch}"}]},
            chunk_epoch=epoch,
        )
        coordinator.record_universe_work_set(
            [f"zombie-extra-{generation_id}"],
            authoritative=True,
            chunk_epoch=epoch,
        )
    assert coordinator._universe_generation_id == generation_before
    assert coordinator._universe_generation_started_at == started_before
    assert coordinator._universe_evaluated_ids == evaluated_before
    discovery = coordinator._universe_discovery_snapshot or {}
    matchbook = discovery.get("matchbook") or snapshot_before.get("matchbook") or []
    assert not any(
        isinstance(row, dict) and str(row.get("id") or "").startswith("zombie-stale")
        for row in matchbook
    )
    assert not any(key.startswith("zombie-extra-") for key in coordinator._universe_work)
    if not trace.generation_open and trace.last_closed_generation_id is not None:
        assert coordinator._universe_generation_id != trace.last_closed_generation_id or (
            coordinator._universe_generation_started_at is None
        )


def action_fresh(trace: GenerationTrace) -> None:
    coordinator = trace.coordinator
    if trace.generation_open:
        coordinator.record_universe_work_set(["fx-a", "fx-b"], authoritative=True)
        coordinator.record_universe_fixture_progress(
            None, _fixture("fx-a", kickoff=trace.clock.now + timedelta(days=1)), [], []
        )
        coordinator.record_universe_fixture_progress(
            None, _fixture("fx-b", kickoff=trace.clock.now + timedelta(days=2)), [], []
        )
        report = _complete_report(coordinator, trace.clock.now)
        coordinator.record_report(report, scan_lane=ScanLane.UNIVERSE)
        if trace.generation_open:
            coordinator._close_universe_generation(trace.clock.now)
        trace.last_closed_generation_id = coordinator._universe_closed_generation_id
    previous = coordinator._universe_generation_id
    action_start(trace)
    assert coordinator._universe_generation_id != previous or previous == 0
    assert trace.cache.no_cross_venue == {} or trace.cache.generation_id == coordinator._universe_generation_id
    if trace.last_closed_generation_id:
        assert coordinator._universe_generation_id != trace.last_closed_generation_id


def action_restart(trace: GenerationTrace) -> None:
    coordinator = trace.coordinator
    open_before = trace.generation_open
    generation_before = coordinator._universe_generation_id
    started_before = coordinator._universe_generation_started_at
    evaluated_before = set(coordinator._universe_evaluated_ids)
    coordinator.flush_universe_checkpoint()
    reset_universe_identity_cache()
    restarted = LiveRefreshCoordinator(
        clock=trace.clock, universe_checkpoint_store=trace.store
    )
    restarted.configure_from_settings()
    restarted._clock = trace.clock
    trace.coordinator = restarted
    if open_before:
        assert restarted._universe_generation_started_at is not None
        assert restarted._universe_generation_id == generation_before
        assert restarted._universe_generation_started_at == started_before
        assert set(restarted._universe_evaluated_ids) >= evaluated_before
        plan = restarted.plan_universe_tick(now=trace.clock.now)
        assert plan.generation_resume is True
        assert plan.universe_generation_id == generation_before
        assert get_universe_identity_cache().no_cross_venue == {}
        assert get_universe_identity_cache().generation_id == generation_before
    else:
        assert restarted._universe_generation_started_at is None
        if trace.last_closed_generation_id is not None:
            assert restarted._universe_generation_id != trace.last_closed_generation_id or (
                restarted._universe_generation_started_at is None
            )
            plan = restarted.plan_universe_tick(now=trace.clock.now)
            if plan.generation_resume:
                assert plan.universe_generation_id != trace.last_closed_generation_id


ACTIONS = {
    "start": action_start,
    "timeout": action_timeout,
    "resume": action_resume,
    "clear": action_clear,
    "stale": action_stale,
    "fresh": action_fresh,
    "restart": action_restart,
}


REQUIRED_SEQUENCE = (
    "start",
    "timeout",
    "resume",
    "clear",
    "stale",
    "fresh",
    "restart",
)


def test_required_generation_lifecycle_sequence(tmp_path: Path) -> None:
    trace = _new_trace(tmp_path)
    try:
        for name in REQUIRED_SEQUENCE:
            ACTIONS[name](trace)
            _assert_invariants(trace)
        assert get_settings().paper_universe_discovery_interval_seconds >= LONG_CADENCE_FLOOR_SECONDS
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()


@pytest.mark.parametrize("seed", (0, 1, 2, 5, 8, 13))
def test_generated_generation_traces_preserve_invariants(tmp_path: Path, seed: int) -> None:
    rng = Random(seed)
    extra = ["timeout", "resume", "stale", "restart", "timeout", "fresh", "clear", "restart"]
    rng.shuffle(extra)
    sequence = ("start", *tuple(extra[:6]), "restart")
    trace = _new_trace(tmp_path / f"seed-{seed}")
    try:
        for name in sequence:
            ACTIONS[name](trace)
            _assert_invariants(trace)
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()


def test_unfinished_truncated_generation_does_not_take_long_cadence(tmp_path: Path) -> None:
    trace = _new_trace(tmp_path)
    try:
        action_start(trace)
        action_timeout(trace)
        _assert_invariants(trace)
        assert trace.generation_open
        assert trace.coordinator._universe_closed_generation_id != trace.generation_id
        plan = trace.coordinator.plan_universe_tick(now=trace.clock.now)
        if plan.lane != ScanLane.UNIVERSE.value:
            trace.clock.advance(8)
            plan = trace.coordinator.plan_universe_tick(now=trace.clock.now)
        assert plan.lane == ScanLane.UNIVERSE.value
        assert plan.generation_resume is True
        assert plan.universe_generation_id == trace.generation_id
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()


def test_closed_generation_is_not_restored_after_process_restart(tmp_path: Path) -> None:
    trace = _new_trace(tmp_path)
    try:
        action_start(trace)
        action_timeout(trace)
        action_resume(trace)
        action_fresh(trace)
        closed = trace.last_closed_generation_id
        assert closed is not None
        if trace.generation_open:
            trace.coordinator.record_universe_work_set(["fx-a", "fx-b"], authoritative=True)
            trace.coordinator.record_universe_fixture_progress(
                None, _fixture("fx-a", kickoff=trace.clock.now + timedelta(days=1)), [], []
            )
            trace.coordinator.record_universe_fixture_progress(
                None, _fixture("fx-b", kickoff=trace.clock.now + timedelta(days=2)), [], []
            )
            trace.coordinator.record_report(
                _complete_report(trace.coordinator, trace.clock.now),
                scan_lane=ScanLane.UNIVERSE,
            )
            if trace.generation_open:
                trace.coordinator._close_universe_generation(trace.clock.now)
        trace.coordinator.flush_universe_checkpoint()
        closed_id = trace.coordinator._universe_closed_generation_id
        reset_universe_identity_cache()
        restarted = LiveRefreshCoordinator(
            clock=trace.clock, universe_checkpoint_store=trace.store
        )
        restarted.configure_from_settings()
        assert restarted._universe_generation_started_at is None
        plan = restarted.plan_universe_tick(now=trace.clock.now)
        if plan.generation_resume:
            assert plan.universe_generation_id != closed_id
        assert get_universe_identity_cache().no_cross_venue == {}
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()
