"""Issue #203: Wave I release hardening regressions.

Data class: deterministic fixture/demo current-state and paper-scan payloads.
Not live, historical, or modelled venue quotes.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sports_hedge.api import paper as paper_api
from sports_hedge.application.collector import (
    DEFAULT_MAX_EVENT_PAIRS,
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementScope
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.venues import (
    PolymarketNormalizer,
    _kalshi_settlement,
    classify_settlement_wording,
)
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from test_issue200_universe_hot_promotion import (
    CANONICAL_ID,
    DISTANT_KICKOFF,
    NOW,
    _fixture,
    _market_row,
    _report,
)
from test_read_only_collector import FakeMatchbook, FakePolymarket, KICKOFF
from test_step8f_automatic_paper_entry import (
    FX as AUTOFILL_FX,
    _ops_bundle,
    _standing,
)
from venue_cost_helpers import matchbook_polymarket_costs


# ---------------------------------------------------------------------------
# 1. Settlement wording fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "scope", "extra_time", "penalties"),
    [
        (
            "Resolves including extra time.",
            SettlementScope.INCLUDING_EXTRA_TIME,
            True,
            False,
        ),
        (
            "Resolves not including extra time.",
            SettlementScope.REGULATION_TIME,
            False,
            False,
        ),
        (
            "Does not include extra time.",
            SettlementScope.REGULATION_TIME,
            False,
            False,
        ),
        (
            "Winner including extra time and penalties.",
            SettlementScope.INCLUDING_PENALTIES,
            True,
            True,
        ),
        (
            "Resolves including penalties after extra time.",
            SettlementScope.INCLUDING_PENALTIES,
            True,
            True,
        ),
        (
            "Resolves based on 90 minutes of regulation time.",
            SettlementScope.REGULATION_TIME,
            False,
            False,
        ),
        (
            "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
            SettlementScope.REGULATION_TIME,
            False,
            False,
        ),
        (
            "Not including extra time and penalties.",
            SettlementScope.REGULATION_TIME,
            False,
            False,
        ),
        (
            "Winner including extra time and penalties. Extra time and penalties do not count.",
            SettlementScope.UNKNOWN,
            None,
            None,
        ),
        ("Ambiguous compound extra-time wording that cannot be mapped.", SettlementScope.UNKNOWN, None, None),
    ],
)
def test_settlement_wording_fail_closed(
    text: str,
    scope: SettlementScope,
    extra_time: bool | None,
    penalties: bool | None,
) -> None:
    classified_scope, classified_et, classified_pen = classify_settlement_wording(text)
    assert classified_scope is scope
    assert classified_et is extra_time
    assert classified_pen is penalties


def test_polymarket_and_kalshi_parsers_use_fail_closed_wording() -> None:
    pm = PolymarketNormalizer()
    event = pm.normalize_event(
        {
            "id": "poly-event-1",
            "title": "Newcastle United vs. Arsenal",
            "startTime": "2026-09-20T15:00:00Z",
            "series": [{"title": "Premier League"}],
        }
    )
    negated = pm.normalize_market(
        event,
        {
            "id": "negated",
            "question": "Will Newcastle United win?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "description": "Resolves not including extra time.",
        },
    )
    assert negated.settlement.scope is SettlementScope.REGULATION_TIME
    assert negated.settlement.extra_time_included is False

    compound = pm.normalize_market(
        event,
        {
            "id": "compound",
            "question": "Will Newcastle United win?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "description": "Winner including extra time and penalties.",
        },
    )
    assert compound.settlement.scope is SettlementScope.INCLUDING_PENALTIES
    assert compound.settlement.penalties_included is True
    assert compound.settlement.extra_time_included is True

    kalshi = _kalshi_settlement(
        {"rules_primary": "Resolves not including extra time.", "ticker": "KXEPL-1"},
        series=None,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert kalshi.scope is SettlementScope.REGULATION_TIME
    assert kalshi.extra_time_included is False
    compound_k = _kalshi_settlement(
        {
            "rules_primary": "Winner including extra time and penalties.",
            "ticker": "KXEPL-2",
        },
        series=None,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert compound_k.scope is SettlementScope.INCLUDING_PENALTIES
    assert compound_k.penalties_included is True


def test_unknown_settlement_wording_refuses_equivalence() -> None:
    scope, extra, penalties = classify_settlement_wording(
        "Settles on some committee decision after the match."
    )
    assert scope is SettlementScope.UNKNOWN
    assert extra is None
    assert penalties is None


# ---------------------------------------------------------------------------
# 2. Cooperative clustering deadline
# ---------------------------------------------------------------------------


class ManyEventsMatchbook(FakeMatchbook):
    PAIRS = (
        ("Arsenal", "Chelsea"),
        ("Liverpool", "Everton"),
        ("Newcastle United", "Tottenham Hotspur"),
        ("Manchester City", "Manchester United"),
        ("Brighton", "Brentford"),
        ("Fulham", "Aston Villa"),
        ("West Ham", "Crystal Palace"),
        ("Bournemouth", "Fulham"),
    )

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": 2000 + index,
                    "name": f"{home} vs {away}",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                }
                for index, (home, away) in enumerate(self.PAIRS)
            ]
        }


class ManyEventsPolymarket(FakePolymarket):
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": f"pm-{index}",
                "title": f"{home} vs. {away}",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
            }
            for index, (home, away) in enumerate(ManyEventsMatchbook.PAIRS)
        ]


@pytest.mark.asyncio
async def test_expensive_clustering_returns_inside_cycle_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_match = EventMatcher.match

    def slow_match(self, left, right, **kwargs):
        time.sleep(0.05)
        return real_match(self, left, right, **kwargs)

    monkeypatch.setattr(EventMatcher, "match", slow_match)
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=ManyEventsMatchbook(),
        polymarket=ManyEventsPolymarket(),
        paper_scan=PaperScanService(intelligence),
    )
    started = time.monotonic()
    report = await collector.collect_and_scan(
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=AUTOFILL_FX,
        cycle_timeout_seconds=0.45,
        max_event_pairs=DEFAULT_MAX_EVENT_PAIRS,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    leftovers = [
        item
        for item in report.discovered_fixtures
        if item.market_evaluation_state == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
    ]
    assert leftovers or any(
        issue.detail == "scan_cycle_deadline_reached" for issue in report.issues
    )
    diagnostics = report.scan_diagnostics or {}
    assert diagnostics.get("soft_deadline_reached") or leftovers
    assert report.discovered_fixtures, "deadline must leftover fixtures, not drop coverage"


# ---------------------------------------------------------------------------
# 3. Coordinator live-refresh lost-update
# ---------------------------------------------------------------------------


def test_live_refresh_status_does_not_lose_last_error_under_concurrent_writes() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    started = NOW
    finished = NOW + timedelta(seconds=1)
    barrier = threading.Barrier(3)
    seen: list[str | None] = []

    def writer() -> None:
        barrier.wait()
        for index in range(60):
            coordinator._mark_lane_error(
                ScanLane.HOT, started, finished, f"scan_failed_{index}"
            )

    def reader() -> None:
        barrier.wait()
        for _ in range(60):
            coordinator.configure_from_settings()
            status = coordinator.public_status()
            seen.append(status.last_error)
            seen.append(status.hot.last_error)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = coordinator.public_status()
    assert final.last_error is not None
    assert str(final.last_error).startswith("scan_failed_")
    assert final.hot.last_error == final.last_error
    assert None not in seen[-10:] or final.last_error is not None


# ---------------------------------------------------------------------------
# 4. FixtureCurrentStateStore concurrent safety + tombstones + promotion
# ---------------------------------------------------------------------------


def test_fixture_store_concurrent_read_write_prune_preserves_promotion_and_tombstones() -> None:
    store = FixtureCurrentStateStore()
    qualifying = _fixture(
        CANONICAL_ID,
        kickoff=DISTANT_KICKOFF,
        arb=True,
        qualifying=1,
        opportunity="qualifying",
    )
    store.upsert_from_report(
        _report(
            [qualifying],
            scan_lane=ScanLane.UNIVERSE.value,
            markets={CANONICAL_ID: [_market_row()]},
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert CANONICAL_ID in store.hot_identity_scope(NOW)
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def writer_hot() -> None:
        barrier.wait()
        try:
            for index in range(40):
                when = NOW + timedelta(milliseconds=index)
                store.upsert_from_report(
                    _report(
                        [qualifying],
                        when=when,
                        scan_lane=ScanLane.HOT.value,
                        markets={CANONICAL_ID: [_market_row()]},
                    ),
                    scan_lane=ScanLane.HOT,
                    now=when,
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def writer_universe() -> None:
        barrier.wait()
        try:
            for index in range(40):
                when = NOW + timedelta(milliseconds=index + 200)
                store.upsert_from_report(
                    _report(
                        [qualifying],
                        when=when,
                        scan_lane=ScanLane.UNIVERSE.value,
                        markets={CANONICAL_ID: [_market_row()]},
                    ),
                    scan_lane=ScanLane.UNIVERSE,
                    now=when,
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        barrier.wait()
        try:
            for index in range(40):
                when = NOW + timedelta(seconds=index)
                store.inventory(when)
                store.hot_identity_scope(when)
                store.membership_counts(when)
                store.detail(CANONICAL_ID, now=when)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def tombstone_writer() -> None:
        barrier.wait()
        try:
            graded = _fixture(
                "qpr-boro",
                kickoff=NOW - timedelta(hours=1),
                fixture_status="graded",
            )
            store.upsert_from_report(
                _report([graded], scan_lane=ScanLane.HOT.value),
                scan_lane=ScanLane.HOT,
                now=NOW,
            )
            later = NOW + timedelta(minutes=5)
            stale = _fixture(
                "qpr-boro",
                kickoff=NOW - timedelta(hours=1),
                fixture_status="open",
            )
            store.upsert_from_report(
                _report([stale], when=later, scan_lane=ScanLane.UNIVERSE.value),
                scan_lane=ScanLane.UNIVERSE,
                now=later,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer_hot),
        threading.Thread(target=writer_universe),
        threading.Thread(target=reader),
        threading.Thread(target=tombstone_writer),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert CANONICAL_ID in store.hot_identity_scope(NOW + timedelta(seconds=1))
    tombstone = store.tombstone_for("qpr-boro")
    assert tombstone is not None
    assert store.inventory(NOW + timedelta(minutes=6))
    assert all(item.canonical_event_id != "qpr-boro" for item in store.inventory(NOW))


# ---------------------------------------------------------------------------
# 5. Manual request kwargs must not become scheduled config
# ---------------------------------------------------------------------------


def test_manual_collect_kwargs_do_not_poison_scheduled_scan_config() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    unusual = {
        "max_event_pairs": 1,
        "minimum_net_edge": "0.5",
        "maximum_execution_risk": 10,
        "assumed_latency_ms": 9_000,
    }
    coordinator.remember_request(unusual)
    scheduled = paper_api.scheduled_collection_kwargs()
    assert scheduled["max_event_pairs"] == DEFAULT_MAX_EVENT_PAIRS == 60
    assert Decimal(str(scheduled["minimum_net_edge"])) == Decimal("0.005")
    assert scheduled["maximum_execution_risk"] == 60
    assert scheduled["assumed_latency_ms"] == 500
    tick_src = inspect.getsource(paper_api.server_owned_refresh_tick)
    assert "last_request()" not in tick_src
    assert "scheduled_collection_kwargs()" in tick_src
    plan = coordinator.plan_tick(now=NOW)
    assert plan.lane in {"hot", "universe", "idle"}


# ---------------------------------------------------------------------------
# 6. Same-lane snapshot clobber
# ---------------------------------------------------------------------------


def test_older_same_lane_observation_cannot_clobber_newer_snapshot() -> None:
    store = FixtureCurrentStateStore()
    newer_when = NOW + timedelta(seconds=8)
    older_when = NOW + timedelta(seconds=2)
    fixture = _fixture(CANONICAL_ID, kickoff=DISTANT_KICKOFF, arb=True, qualifying=1)
    store.upsert_from_report(
        _report(
            [fixture],
            when=newer_when,
            scan_lane=ScanLane.HOT.value,
            markets={CANONICAL_ID: [_market_row()]},
        ),
        scan_lane=ScanLane.HOT,
        now=newer_when,
    )
    store.upsert_from_report(
        _report(
            [fixture],
            when=older_when,
            scan_lane=ScanLane.HOT.value,
            markets={CANONICAL_ID: [_market_row(edge=Decimal("0.011"))]},
        ),
        scan_lane=ScanLane.HOT,
        now=older_when,
    )
    rows = store.current_radar_rows(newer_when + timedelta(seconds=1))
    assert rows
    assert rows[0].last_scanned_at == newer_when

    store.upsert_from_report(
        _report(
            [fixture],
            when=newer_when + timedelta(seconds=4),
            scan_lane=ScanLane.UNIVERSE.value,
            markets={CANONICAL_ID: [_market_row()]},
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=newer_when + timedelta(seconds=4),
    )
    store.upsert_from_report(
        _report(
            [fixture],
            when=newer_when + timedelta(seconds=1),
            scan_lane=ScanLane.UNIVERSE.value,
            markets={CANONICAL_ID: [_market_row()]},
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=newer_when + timedelta(seconds=1),
    )
    later_rows = store.current_radar_rows(newer_when + timedelta(seconds=5))
    assert later_rows
    # HOT membership reports HOT due-time, not the UNIVERSE snapshot clock.
    assert later_rows[0].last_scanned_at == newer_when
    record = store._rows[CANONICAL_ID]
    assert record.hot is not None
    assert record.hot.last_scanned_at == newer_when
    assert record.universe is not None
    assert record.universe.last_scanned_at == newer_when + timedelta(seconds=4)


# ---------------------------------------------------------------------------
# 7. Persist retry audit duplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_retry_does_not_duplicate_audit_but_new_scans_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        from test_step8f_automatic_paper_entry import _matchbook_btts, _polymarket_btts

        decision = scan.scan_pair(
            _matchbook_btts(),
            _polymarket_btts(),
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
        coordinator = LiveRefreshCoordinator()
        coordinator.reset()
        from test_hot_scan_reliability import _hot_leftover_report

        report = _hot_leftover_report(cancelled=False).model_copy(
            update={"paper_decisions": [decision]}
        )

        def operations_factory(watchlist_arg=None, alerts=None):
            del alerts
            if watchlist_arg is not None:
                ops.watchlist = watchlist_arg
            return ops

        monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
        real_persist = paper_api._persist_decision
        persist_calls = {"n": 0}

        def persist_after_open_then_fail_once(decision_arg: Any, **kwargs: Any) -> None:
            real_persist(decision_arg, **kwargs)
            persist_calls["n"] += 1
            if persist_calls["n"] == 1:
                raise RuntimeError("audit_write_failed")

        monkeypatch.setattr(paper_api, "_persist_decision", persist_after_open_then_fail_once)
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        assert len(audit.list_scans(limit=100)) == 1
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        assert persist_calls["n"] == 2
        assert len(audit.list_scans(limit=100)) == 1
        opened = ops.list_active_trades()
        assert len(opened) == 1
        assert opened[0].state is PaperTradeState.OPEN

        second = scan.scan_pair(
            _matchbook_btts(),
            _polymarket_btts(),
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        second.paper_audit_record_id = None
        second_report = report.model_copy(update={"paper_decisions": [second]})
        monkeypatch.setattr(paper_api, "_persist_decision", real_persist)
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            second_report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        assert len(audit.list_scans(limit=100)) == 2
        assert len(ops.list_active_trades()) == 1
    finally:
        repository.close()
        ledger.close()
        audit.close()


def test_append_scan_source_stays_insert_not_upsert() -> None:
    source = inspect.getsource(SqlitePaperScanRepository.append_scan)
    assert "ON CONFLICT" not in source
    assert "IntegrityError" in source
