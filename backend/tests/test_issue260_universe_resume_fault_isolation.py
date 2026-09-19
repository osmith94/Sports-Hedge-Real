"""Issue #260: durable UNIVERSE resume and per-market fault isolation.

Deterministic fixtures shaped like existing Matchbook/Kalshi/Polymarket tests.
Not owner-live evidence.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.application.collector import (
    CollectorIssue,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_cycle_audit import (
    build_paper_scan_cycle_record,
    cycle_last_error,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report
from test_issue245_scan_cycle_history import _report as _cycle_report
from venue_cost_helpers import matchbook_kalshi_costs, matchbook_polymarket_costs
from registered_kalshi import FakeKalshi


KICKOFF = NOW + timedelta(days=2)


def _universe_roster(count: int = 72):
    return [
        _fixture(f"ev-{index:03d}", kickoff=KICKOFF + timedelta(minutes=index))
        for index in range(count)
    ]


def _partial_chunk(evaluated_ids: list[str], leftover_ids: list[str], *, when):
    fixtures = [
        _fixture(event_id, kickoff=KICKOFF, evaluation="evaluated")
        for event_id in evaluated_ids
    ]
    fixtures.extend(
        _fixture(
            event_id,
            kickoff=KICKOFF + timedelta(minutes=1),
            evaluation="not_evaluated_scan_deadline",
        )
        for event_id in leftover_ids
    )
    return _report(fixtures, when=when, scan_lane=ScanLane.UNIVERSE.value).model_copy(
        update={"completed_at": when + timedelta(seconds=4)}
    )


@pytest.mark.asyncio
async def test_seventy_fixture_universe_failure_does_not_reprocess_evaluated_ids() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    roster = _universe_roster(72)
    coordinator.record_report(_report(roster, when=NOW), scan_lane=ScanLane.UNIVERSE)
    coordinator._next_hot_due = NOW + timedelta(seconds=1_000)
    coordinator._next_universe_due = NOW
    coordinator._universe_generation_started_at = None
    coordinator._universe_work_used = 0.0
    coordinator._universe_evaluated_ids = set()
    coordinator._universe_cursor = None
    evaluated = [item.canonical_event_id for item in roster[:5]]
    leftover = [item.canonical_event_id for item in roster[5:]]

    async def partial_runner():
        return _partial_chunk(evaluated, leftover, when=clock.now)

    first = coordinator.plan_tick(now=clock.now)
    assert first.lane == "universe"
    generation = first.universe_generation_id
    await coordinator.run_cycle(
        partial_runner,
        timeout_seconds=first.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    assert set(evaluated) <= coordinator._universe_evaluated_ids

    async def fail_runner():
        clock.advance(15)
        raise RuntimeError("list_events_timeout after 15s")

    second = coordinator.plan_tick(now=clock.now)
    assert second.lane == "universe"
    with pytest.raises(RuntimeError, match="list_events_timeout"):
        await coordinator.run_cycle(
            fail_runner,
            timeout_seconds=second.coordinator_timeout_seconds,
            scan_lane=ScanLane.UNIVERSE,
        )
    assert coordinator._universe_generation_id == generation
    assert set(evaluated) <= coordinator._universe_evaluated_ids
    clock.now = coordinator._universe_retry_at or clock.now
    coordinator._next_hot_due = clock.now + timedelta(seconds=1_000)
    resumed = coordinator.plan_tick(now=clock.now)
    assert resumed.lane == "universe"
    assert resumed.universe_generation_id == generation
    assert resumed.generation_resume is True
    assert set(evaluated) <= set(resumed.skip_event_ids)
    assert not set(leftover) & set(resumed.skip_event_ids)


@pytest.mark.asyncio
async def test_successful_work_budget_exhaustion_pauses_same_generation() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    roster = _universe_roster(72)
    first_ids = [item.canonical_event_id for item in roster[:8]]
    leftover_ids = [item.canonical_event_id for item in roster[8:]]
    coordinator._next_hot_due = NOW + timedelta(seconds=1_000)
    coordinator._next_universe_due = NOW

    async def first_chunk():
        return _partial_chunk(first_ids, leftover_ids, when=clock.now)

    plan = coordinator.plan_tick(now=clock.now)
    await coordinator.run_cycle(
        first_chunk,
        timeout_seconds=plan.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    generation = coordinator._universe_generation_id
    coordinator._universe_work_used = 148.0
    heavy = _partial_chunk(first_ids, leftover_ids, when=clock.now).model_copy(
        update={"completed_at": clock.now + timedelta(seconds=5)}
    )
    coordinator.record_report(heavy, scan_lane=ScanLane.UNIVERSE)
    assert coordinator._universe_generation_started_at is not None
    assert coordinator._universe_budget_paused is False
    assert coordinator._universe_generation_id == generation
    assert set(first_ids) <= coordinator._universe_evaluated_ids
    continued = coordinator.plan_tick(now=clock.now)
    assert continued.lane == "universe"
    assert continued.reason != "universe_budget_paused"
    assert continued.universe_generation_id == generation
    assert continued.generation_resume is True
    assert continued.resume_cursor == coordinator._universe_cursor
    assert set(first_ids) <= set(continued.skip_event_ids)


def test_process_restart_restores_open_generation_from_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "paper_settings.sqlite"
    store = SqliteUniverseCheckpointStore(database)
    clock = FakeClock(NOW)
    first = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    first.configure_from_settings()
    roster = _universe_roster(72)
    evaluated = [item.canonical_event_id for item in roster[:6]]
    leftover = [item.canonical_event_id for item in roster[6:]]
    first._next_hot_due = NOW + timedelta(seconds=1_000)
    first._next_universe_due = NOW
    plan = first.plan_tick(now=NOW)
    assert plan.lane == "universe"
    first._mark_lane_started(ScanLane.UNIVERSE, NOW)
    first.record_report(
        _partial_chunk(evaluated, leftover, when=NOW),
        scan_lane=ScanLane.UNIVERSE,
    )
    generation = first._universe_generation_id
    cursor = first._universe_cursor
    assert store.load() is not None

    restarted = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_generation_id == generation
    assert restarted._universe_generation_started_at is not None
    assert set(evaluated) <= restarted._universe_evaluated_ids
    assert restarted._universe_cursor == cursor
    restarted._next_hot_due = clock.now + timedelta(seconds=1_000)
    resumed = restarted.plan_tick(now=clock.now)
    assert resumed.lane == "universe"
    assert resumed.universe_generation_id == generation
    assert resumed.generation_resume is True
    assert set(evaluated) <= set(resumed.skip_event_ids)


def test_explicit_reset_clears_universe_checkpoint(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "reset.sqlite")
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    coordinator.configure_from_settings()
    coordinator._next_hot_due = NOW + timedelta(seconds=1_000)
    coordinator._next_universe_due = NOW
    coordinator._mark_lane_started(ScanLane.UNIVERSE, NOW)
    coordinator.record_report(
        _partial_chunk(["ev-000"], ["ev-001"], when=NOW),
        scan_lane=ScanLane.UNIVERSE,
    )
    assert store.load() is not None
    coordinator.reset()
    assert store.load() is None
    assert coordinator._universe_generation_started_at is None
    assert coordinator._universe_evaluated_ids == set()
    coordinator._clock = clock
    clock.now = NOW
    coordinator._next_hot_due = NOW + timedelta(seconds=1_000)
    coordinator._next_universe_due = NOW
    nxt = coordinator.plan_tick(now=NOW)
    assert nxt.lane == "universe"
    assert nxt.generation_resume is False
    assert nxt.skip_event_ids == []


def test_unsupported_matchbook_markets_are_not_cycle_last_error() -> None:
    report = _cycle_report(
        started_at=NOW,
        scan_lane=ScanLane.UNIVERSE.value,
        fixtures=[_fixture("ev-1x2", kickoff=KICKOFF)],
        matched_event_pairs=37,
        matched_market_pairs=0,
    ).model_copy(
        update={
            "issues": [
                CollectorIssue(
                    stage="normalize_market",
                    venue=VenueName.MATCHBOOK,
                    source_id="total-1",
                    detail="Unsupported Matchbook market: Total",
                ),
                CollectorIssue(
                    stage="normalize_market",
                    venue=VenueName.MATCHBOOK,
                    source_id="ht-total-1",
                    detail="Unsupported Matchbook market: 1st Half Total",
                ),
                CollectorIssue(
                    stage="collect",
                    detail="scan_cycle_deadline_reached",
                ),
            ]
        }
    )
    row = build_paper_scan_cycle_record(report, scan_lane=ScanLane.UNIVERSE)
    assert row.last_error is None
    assert cycle_last_error(report) is None
    assert "unsupported markets skipped" in (row.operator_summary or "")


def test_provider_timeout_issue_remains_cycle_last_error() -> None:
    report = _cycle_report(
        started_at=NOW,
        scan_lane=ScanLane.UNIVERSE.value,
        fixtures=[_fixture("ev-timeout", kickoff=KICKOFF)],
        venue_health={"matchbook": "timeout", "polymarket": "ok", "kalshi": "ok"},
    ).model_copy(
        update={
            "issues": [
                CollectorIssue(
                    stage="normalize_market",
                    venue=VenueName.MATCHBOOK,
                    detail="Unsupported Matchbook market: Total",
                ),
                CollectorIssue(
                    stage="list_events",
                    venue=VenueName.MATCHBOOK,
                    detail="list_events_timeout after 15s",
                ),
            ]
        }
    )
    row = build_paper_scan_cycle_record(report, scan_lane=ScanLane.UNIVERSE)
    assert row.last_error == "list_events_timeout after 15s"
    assert row.degraded is True


class _MatchResultMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": 8101,
                    "name": "Newcastle United vs Arsenal",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                },
                {
                    "id": 8199,
                    "name": "Outright Premier League winner",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                },
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        if str(event_id) != "8101":
            return {"markets": []}
        return {
            "markets": [
                {
                    "id": 9101,
                    "name": "Match Odds",
                    "runners": [
                        {
                            "id": 1,
                            "name": "Newcastle United",
                            "prices": [
                                {"side": "back", "odds": "2.10", "available-amount": "80"}
                            ],
                        },
                        {
                            "id": 2,
                            "name": "Draw",
                            "prices": [
                                {"side": "back", "odds": "3.40", "available-amount": "80"}
                            ],
                        },
                        {
                            "id": 3,
                            "name": "Arsenal",
                            "prices": [
                                {"side": "back", "odds": "3.60", "available-amount": "80"}
                            ],
                        },
                    ],
                },
                {
                    "id": 9102,
                    "name": "Total",
                    "runners": [{"id": 11, "name": "Over 2.5"}, {"id": 12, "name": "Under 2.5"}],
                },
                {
                    "id": 9103,
                    "name": "1st Half Total",
                    "runners": [{"id": 21, "name": "Over 1.5"}, {"id": 22, "name": "Under 1.5"}],
                },
            ]
        }


class _MatchResultPolymarket:
    def __init__(self) -> None:
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-new-ars-1x2",
                "title": "Newcastle United vs Arsenal",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return [
            {
                "id": "pm-1x2",
                "question": "Match result?",
                "sportsMarketType": "moneyline",
                "outcomes": '["Newcastle United", "Draw", "Arsenal"]',
                "clobTokenIds": '["h", "d", "a"]',
                "description": "Resolves based on 90 minutes of regulation time.",
                "feesEnabled": False,
            }
        ]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        token = str(outcome_id)
        now_ms = int((NOW.timestamp()) * 1000)
        return {
            "asset_id": token,
            "timestamp": now_ms - 150,
            "bids": [{"price": "0.30", "size": "200"}],
            "asks": [{"price": "0.32", "size": "200"}],
        }


@pytest.mark.asyncio
async def test_resumed_universe_still_counts_equivalent_match_result_pair() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_MatchResultMatchbook(),
        polymarket=_MatchResultPolymarket(),
        kalshi=FakeKalshi(
            [("Premier League", "Newcastle United", "Arsenal", KICKOFF)],
            families=("GAME",),
        ),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        baseline = await collector.collect_and_scan(
            venue_costs=matchbook_kalshi_costs() + matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            generation_resume=False,
            universe_generation_id=1,
        )
        assert baseline.matched_event_pairs >= 1
        assert baseline.matched_market_pairs >= 1
        unsupported = [
            issue
            for issue in baseline.issues
            if issue.stage == "normalize_market"
            and "Unsupported Matchbook market:" in issue.detail
        ]
        assert any("Total" in issue.detail for issue in unsupported)
        assert cycle_last_error(baseline) is None
        ids = [item.canonical_event_id for item in baseline.discovered_fixtures]
        skip = [event_id for event_id in ids if "8199" in event_id]
        resumed = await collector.collect_and_scan(
            venue_costs=matchbook_kalshi_costs() + matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            skip_event_ids=skip,
            generation_resume=True,
            universe_generation_id=1,
        )
        assert resumed.matched_event_pairs >= 1
        assert resumed.matched_market_pairs >= 1
        assert resumed.scan_diagnostics["stale_generation_state_ignored"] is False
        assert cycle_last_error(resumed) is None
        assert any(
            issue.detail.startswith("Unsupported Matchbook market:")
            for issue in resumed.issues
        )
    finally:
        repository.close()
