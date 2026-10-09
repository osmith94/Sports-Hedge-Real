"""Issue #27: read-only Price-2 Activity feed projection.

PAPER / recorded audits only. No provider calls, scanner changes, or capture writes.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.execution_reprice import capture_with_execution_reprice
from sports_hedge.arbitrage.watchlist.models import (
    LifecycleEventType,
    OpportunityLifecycleEvent,
    OpportunityStatus,
    WatchLeg,
    WatchObservation,
)
from sports_hedge.arbitrage.watchlist.price2_activity import (
    PRICE2_ACTIVITY_MAX_OPPORTUNITY_IDS,
    project_audit_row,
    project_lifecycle_rejection,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName

OBSERVED = datetime(2026, 10, 9, 19, 10, tzinfo=UTC)
PRICE2_START = datetime(2026, 10, 9, 19, 11, 41, 709000, tzinfo=UTC)
PRICE2_END = datetime(2026, 10, 9, 19, 11, 42, 521000, tzinfo=UTC)


def _legs() -> list[WatchLeg]:
    return [
        WatchLeg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-1",
            currency="GBP",
            native_stake=Decimal(3),
            gbp_per_unit=Decimal(1),
            gbp_stake=Decimal(3),
            net_decimal_odds=Decimal("2.10"),
            cumulative_depth_gbp=Decimal(3),
        ),
        WatchLeg(
            outcome="away",
            venue=VenueName.POLYMARKET,
            source_market_id="pm-1",
            currency="USD",
            native_stake=Decimal(4),
            gbp_per_unit=Decimal("0.75"),
            gbp_stake=Decimal(3),
            net_decimal_odds=Decimal("1.95"),
            cumulative_depth_gbp=Decimal(3),
        ),
    ]


def _observe(service: WatchlistService, market_id: str, **overrides) -> None:
    payload = {
        "observed_at": OBSERVED,
        "canonical_event_id": f"evt-{market_id}",
        "canonical_market_id": market_id,
        "competition": "Primeira Liga",
        "home_team": "Moreirense FC",
        "away_team": "Gil Vicente",
        "market_family": MarketFamily.MATCH_RESULT,
        "period": FootballPeriod.FULL_TIME,
        "legs": _legs(),
        "trigger_net_edge": Decimal("0.01"),
        "current_net_edge": Decimal("0.6631"),
        "implied_probability_sum": Decimal("0.60"),
        "solver_is_arbitrage": True,
        "eligible_for_paper_simulation": True,
        "quote_age_ms": 800,
        "limiting_depth_gbp": Decimal(3),
        "guaranteed_profit_gbp": Decimal("1.99"),
        "venues": [VenueName.MATCHBOOK, VenueName.POLYMARKET],
    }
    payload.update(overrides)
    service.observe(WatchObservation(**payload))


def _snapshot_json(
    *,
    snapshot_id: str,
    accepted: bool,
    net_edge: str = "0.012",
    guaranteed_profit: str = "0.04",
    include_legs: bool = True,
    include_timing: bool = True,
    rejection_reason: str | None = None,
    cycle: int = 1,
) -> str:
    payload = {
        "snapshot_id": snapshot_id,
        "execution_cycle": cycle,
        "started_at": PRICE2_START.isoformat(),
        "evaluated_at": PRICE2_END.isoformat(),
        "skew_ms": 40,
        "oldest_quote_age_ms": 180,
        "net_edge": net_edge,
        "guaranteed_profit": guaranteed_profit,
        "accepted": accepted,
        "rejection_reason": rejection_reason,
        "frozen_orders": [
            {
                "venue": "matchbook",
                "currency": "GBP",
                "approved_stake": "3",
                "approved_decimal_odds": "2.04",
            },
            {
                "venue": "polymarket",
                "currency": "USD",
                "approved_stake": "4",
                "approved_decimal_odds": "1.90",
            },
        ],
    }
    if include_legs:
        payload["legs"] = [
            {
                "venue": "matchbook",
                "outcome": "home",
                "displayed_odds": "2.04",
                "available_depth": "80",
                "requested_stake": "3",
                "native_market_id": "mb-1",
                "retrieval_native_id": "mb-1",
                "quote_age_ms": 120,
                "retrieved_at": PRICE2_START.isoformat(),
            },
            {
                "venue": "polymarket",
                "outcome": "away",
                "displayed_odds": "1.90",
                "available_depth": "50",
                "requested_stake": "4",
                "native_market_id": "pm-1",
                "retrieval_native_id": "pm-1",
                "quote_age_ms": 180,
                "retrieved_at": (PRICE2_START + timedelta(milliseconds=40)).isoformat(),
            },
        ]
    if include_timing:
        payload["timing"] = {
            "assembly_ms": 812,
            "calls": [
                {
                    "venue": "matchbook",
                    "stage": "book",
                    "source_id": "mb-1",
                    "outcome": "home",
                    "slot_wait_ms": 12,
                    "io_ms": 40,
                },
                {
                    "venue": "polymarket",
                    "stage": "book",
                    "source_id": "pm-1",
                    "outcome": "away",
                    "slot_wait_ms": 8,
                    "io_ms": 55,
                },
            ],
        }
    return json.dumps(payload)


def _record_audit(
    service: WatchlistService,
    *,
    snapshot_id: str,
    opportunity_id: str,
    accepted: bool,
    occurred_at: datetime,
    cycle: int = 1,
    cycle_outcome: str | None = None,
    include_legs: bool = True,
    include_timing: bool = True,
    net_edge: str = "0.012",
    rejection_reason: str | None = None,
    market_id: str = "mkt-moreirense",
) -> None:
    service.record_execution_snapshot_audit(
        snapshot_id=snapshot_id,
        opportunity_id=opportunity_id,
        catalogue_row_id="cat-1",
        canonical_market_id=market_id,
        occurred_at=occurred_at,
        accepted=accepted,
        rejection_reason=rejection_reason,
        snapshot_json=_snapshot_json(
            snapshot_id=snapshot_id,
            accepted=accepted,
            include_legs=include_legs,
            include_timing=include_timing,
            net_edge=net_edge,
            rejection_reason=rejection_reason,
            cycle=cycle,
        ),
        diagnostics_json=json.dumps(
            {
                "started_at": PRICE2_START.isoformat(),
                "assembly_ms": 812,
                "calls": [
                    {
                        "venue": "matchbook",
                        "stage": "book",
                        "source_id": "mb-1",
                        "outcome": "home",
                        "slot_wait_ms": 12,
                        "io_ms": 40,
                    }
                ],
            }
        ),
        execution_cycle=cycle,
        cycle_outcome=cycle_outcome or ("accepted" if accepted else "rejected"),
    )


def _client(service: WatchlistService) -> TestClient:
    app.dependency_overrides[get_watchlist_service] = lambda: service
    return TestClient(app)


def _trace_sql(repository: SqliteWatchlistRepository) -> list[str]:
    calls: list[str] = []

    def tracer(sql: str) -> None:
        calls.append(sql)

    repository._connection.set_trace_callback(tracer)
    return calls


def test_price2_accepted_is_not_a_fill_and_keeps_price1_edge_separate() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    client = _client(service)
    try:
        _observe(service, "mkt-moreirense")
        opportunity_id = "watch:mkt-moreirense"
        service.repository.append_event(
            OpportunityLifecycleEvent(
                event_id="q-detected",
                opportunity_id=opportunity_id,
                occurred_at=OBSERVED,
                event_type=LifecycleEventType.QUALIFYING_DETECTED,
                status=OpportunityStatus.TRIGGERED,
                current_net_edge=Decimal("0.6631"),
                fixture_label="Moreirense FC v Gil Vicente",
                market_family="match_result",
                detail="solver_qualified",
            )
        )
        _record_audit(
            service,
            snapshot_id="exec:accepted-1",
            opportunity_id=opportunity_id,
            accepted=True,
            occurred_at=PRICE2_END,
            cycle=1,
            cycle_outcome="accepted",
            net_edge="0.012",
        )
        activity = client.get(
            "/paper/watchlist/activity",
            params={"limit": 100, "operator_signal": True},
        )
        assert activity.status_code == 200
        types = [item["event_type"] for item in activity.json()]
        assert "qualifying_detected" in types
        assert "paper_fill_complete" not in types
        price2 = client.get(
            "/paper/watchlist/price2-attempts",
            params={"opportunity_ids": opportunity_id, "limit": 200},
        )
        assert price2.status_code == 200
        rows = price2.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "accepted"
        assert row["filled"] is False
        assert row["net_edge"] == "0.012"
        assert row["net_edge"] != "0.6631"
        assert row["elapsed_ms"] == 812
        assert row["filled"] is False
        assert row["trade_linked"] is False
        assert row["finished_at"].startswith("2026-10-09T19:11:42.521")
        assert row["legs"][0]["displayed_odds"] == "2.04"
        assert row["legs"][0]["displayed_odds"] != "2.10"
        assert "available_depth" not in row["legs"][0]
        assert row["data_kind"] == "historical_recorded"
        assert "snapshot_json" not in row
        assert "frozen_orders" not in row
        assert row["execution_size"] is None
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_qualifying_lost_is_not_a_price2_rejection() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    client = _client(service)
    try:
        _observe(service, "mkt-lost")
        opportunity_id = "watch:mkt-lost"
        service.repository.append_event(
            OpportunityLifecycleEvent(
                event_id="q-lost",
                opportunity_id=opportunity_id,
                occurred_at=OBSERVED + timedelta(seconds=90),
                event_type=LifecycleEventType.QUALIFYING_LOST,
                status=OpportunityStatus.WATCHING,
                current_net_edge=Decimal("0.004"),
                fixture_label="Moreirense FC v Gil Vicente",
                market_family="match_result",
                detail="qualifying lost · net edge below threshold",
            )
        )
        activity = client.get(
            "/paper/watchlist/activity",
            params={"limit": 100, "operator_signal": True},
        ).json()
        assert any(item["event_type"] == "qualifying_lost" for item in activity)
        price2 = client.get(
            "/paper/watchlist/price2-attempts",
            params={"opportunity_ids": opportunity_id},
        ).json()
        assert price2 == []
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_price2_rejection_uses_stored_reason_not_fabricated_odds() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    client = _client(service)
    try:
        _observe(service, "mkt-stale")
        opportunity_id = "watch:mkt-stale"
        _record_audit(
            service,
            snapshot_id="exec:stale-1",
            opportunity_id=opportunity_id,
            accepted=False,
            occurred_at=PRICE2_END,
            rejection_reason="execution_reprice_stale",
            net_edge="0.002",
        )
        row = client.get(
            "/paper/watchlist/price2-attempts",
            params={"opportunity_ids": opportunity_id},
        ).json()[0]
        assert row["status"] == "rejected"
        assert row["rejection_reason"] == "execution_reprice_stale"
        assert row["net_edge"] == "0.002"
        assert row["legs"][0]["displayed_odds"] == "2.04"
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_no_snapshot_lifecycle_rejection_has_no_invented_quotes() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    client = _client(service)
    try:
        _observe(service, "mkt-miss")
        opportunity_id = "watch:mkt-miss"
        service.repository.append_event(
            OpportunityLifecycleEvent(
                event_id="miss-1",
                opportunity_id=opportunity_id,
                occurred_at=PRICE2_END,
                event_type=LifecycleEventType.PAPER_FILL_REJECTED,
                status=OpportunityStatus.TRIGGERED,
                detail="execution_reprice_failed provider timeout",
                fixture_label="Moreirense FC v Gil Vicente",
                market_family="match_result",
            )
        )
        service.repository.append_event(
            OpportunityLifecycleEvent(
                event_id="noise-reject",
                opportunity_id=opportunity_id,
                occurred_at=PRICE2_END + timedelta(seconds=1),
                event_type=LifecycleEventType.PAPER_FILL_REJECTED,
                status=OpportunityStatus.TRIGGERED,
                detail="paper_autofill_disabled",
            )
        )
        rows = client.get(
            "/paper/watchlist/price2-attempts",
            params={"opportunity_ids": opportunity_id},
        ).json()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "incomplete_unavailable"
        assert row["source"] == "lifecycle_rejection"
        assert row["rejection_reason"] == "execution_reprice_failed"
        assert row["legs"] == []
        assert row["net_edge"] is None
        assert row["elapsed_ms"] is None
        assert row["started_at"] is None
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_lifecycle_rejection_with_snapshot_id_is_not_duplicated() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    try:
        _observe(service, "mkt-dup")
        opportunity_id = "watch:mkt-dup"
        _record_audit(
            service,
            snapshot_id="exec:dup-1",
            opportunity_id=opportunity_id,
            accepted=False,
            occurred_at=PRICE2_END,
            rejection_reason="execution_reprice_skew",
        )
        service.repository.append_event(
            OpportunityLifecycleEvent(
                event_id="dup-life",
                opportunity_id=opportunity_id,
                occurred_at=PRICE2_END,
                event_type=LifecycleEventType.PAPER_FILL_REJECTED,
                status=OpportunityStatus.TRIGGERED,
                detail="execution_reprice_skew snapshot_id=exec:dup-1 skew_ms=600",
            )
        )
        rows = service.price2_activity(opportunity_ids=[opportunity_id])
        assert len(rows) == 1
        assert rows[0].snapshot_id == "exec:dup-1"
        assert project_lifecycle_rejection(
            OpportunityLifecycleEvent(
                event_id="dup-life",
                opportunity_id=opportunity_id,
                occurred_at=PRICE2_END,
                event_type=LifecycleEventType.PAPER_FILL_REJECTED,
                status=OpportunityStatus.TRIGGERED,
                detail="execution_reprice_skew snapshot_id=exec:dup-1",
            )
        ) is None
    finally:
        repository.close()


def test_two_cycles_stay_on_one_opportunity_and_chronological() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    client = _client(service)
    try:
        _observe(service, "mkt-cycle")
        _observe(
            service,
            "mkt-other",
            home_team="Other Home",
            away_team="Other Away",
        )
        first = PRICE2_END
        second = PRICE2_END + timedelta(seconds=2)
        _record_audit(
            service,
            snapshot_id="exec:c1",
            opportunity_id="watch:mkt-cycle",
            accepted=True,
            occurred_at=first,
            cycle=1,
        )
        _record_audit(
            service,
            snapshot_id="exec:c2",
            opportunity_id="watch:mkt-cycle",
            accepted=True,
            occurred_at=second,
            cycle=2,
            net_edge="0.009",
        )
        _record_audit(
            service,
            snapshot_id="exec:other",
            opportunity_id="watch:mkt-other",
            accepted=False,
            occurred_at=second + timedelta(seconds=1),
            rejection_reason="execution_reprice_no_longer_qualifying",
            market_id="mkt-other",
        )
        rows = client.get(
            "/paper/watchlist/price2-attempts",
            params={"opportunity_ids": "watch:mkt-cycle", "limit": 200},
        ).json()
        assert [item["snapshot_id"] for item in rows] == ["exec:c2", "exec:c1"]
        assert [item["execution_cycle"] for item in rows] == [2, 1]
        assert all(item["opportunity_id"] == "watch:mkt-cycle" for item in rows)
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_older_audit_without_legs_is_honest() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    try:
        _observe(service, "mkt-old")
        _record_audit(
            service,
            snapshot_id="exec:old",
            opportunity_id="watch:mkt-old",
            accepted=False,
            occurred_at=PRICE2_END,
            include_legs=False,
            include_timing=False,
            rejection_reason="execution_reprice_failed",
        )
        row = project_audit_row(
            service.repository.list_execution_snapshot_audits("watch:mkt-old")[0]
        )
        assert row.legs == []
        assert row.execution_size is None
        assert row.net_edge == "0.012"
        assert row.status == "rejected"
    finally:
        repository.close()


def test_requires_opportunity_ids_and_rejects_global_read() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    client = _client(service)
    try:
        missing = client.get("/paper/watchlist/price2-attempts")
        assert missing.status_code == 422
        empty = client.get(
            "/paper/watchlist/price2-attempts",
            params={"opportunity_ids": "  ,  "},
        )
        assert empty.status_code == 422
        too_many = ",".join(f"watch:mkt-{index}" for index in range(PRICE2_ACTIVITY_MAX_OPPORTUNITY_IDS + 1))
        overflow = client.get(
            "/paper/watchlist/price2-attempts",
            params={"opportunity_ids": too_many},
        )
        assert overflow.status_code == 422
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_bounded_indexed_read_is_not_n_plus_one_or_table_sweep() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    try:
        _observe(service, "visible-a")
        _observe(service, "visible-b")
        for index in range(250):
            _record_audit(
                service,
                snapshot_id=f"exec:noise-{index}",
                opportunity_id=f"watch:noise-{index}",
                accepted=False,
                occurred_at=PRICE2_END - timedelta(seconds=index),
                market_id=f"noise-{index}",
            )
        _record_audit(
            service,
            snapshot_id="exec:vis-a",
            opportunity_id="watch:visible-a",
            accepted=True,
            occurred_at=PRICE2_END,
            market_id="visible-a",
        )
        _record_audit(
            service,
            snapshot_id="exec:vis-b",
            opportunity_id="watch:visible-b",
            accepted=False,
            occurred_at=PRICE2_END,
            rejection_reason="execution_reprice_skew",
            market_id="visible-b",
        )
        calls = _trace_sql(repository)
        rows = service.price2_activity(
            opportunity_ids=["watch:visible-a", "watch:visible-b"],
            since=OBSERVED,
            limit=200,
            include_recent=False,
        )
        repository._connection.set_trace_callback(None)
        assert {item.snapshot_id for item in rows} == {"exec:vis-a", "exec:vis-b"}
        statements = [sql for sql in calls if sql.lstrip().upper().startswith("SELECT")]
        audit_selects = [sql for sql in statements if "FROM execution_snapshot_audits" in sql]
        assert len(audit_selects) == 1
        assert "opportunity_id IN" in audit_selects[0]
        assert "SELECT * FROM execution_snapshot_audits" not in audit_selects[0]
        lifecycle_selects = [
            sql for sql in statements if "FROM watchlist_lifecycle_events" in sql
        ]
        assert len(lifecycle_selects) == 1
        assert len(statements) == 3
        plan = repository._connection.execute(
            f"EXPLAIN QUERY PLAN {audit_selects[0]}"
        ).fetchall()
        plan_text = " ".join(" ".join(str(part) for part in row) for row in plan)
        assert "idx_execution_snapshot_audits_opportunity" in plan_text
        assert "SCAN TABLE execution_snapshot_audits" not in plan_text
    finally:
        repository.close()


def test_two_polymarket_token_calls_are_not_copied_onto_every_leg() -> None:
    row = project_audit_row(
        {
            "snapshot_id": "exec:pm-two",
            "opportunity_id": "watch:mkt-pm",
            "occurred_at": (PRICE2_END + timedelta(seconds=5)).isoformat(),
            "accepted": 1,
            "cycle_outcome": "accepted",
            "snapshot_json": json.dumps(
                {
                    "started_at": PRICE2_START.isoformat(),
                    "evaluated_at": PRICE2_END.isoformat(),
                    "legs": [
                        {
                            "venue": "polymarket",
                            "outcome": "yes",
                            "displayed_odds": "1.90",
                            "requested_stake": "4",
                            "retrieval_native_id": "token-yes",
                            "native_market_id": "token-yes",
                        },
                        {
                            "venue": "polymarket",
                            "outcome": "no",
                            "displayed_odds": "2.10",
                            "requested_stake": "4",
                            "retrieval_native_id": "token-no",
                            "native_market_id": "token-no",
                        },
                    ],
                    "timing": {
                        "assembly_ms": 900,
                        "calls": [
                            {
                                "venue": "polymarket",
                                "source_id": "token-yes",
                                "slot_wait_ms": 5,
                                "io_ms": 20,
                            },
                            {
                                "venue": "polymarket",
                                "source_id": "token-no",
                                "slot_wait_ms": 40,
                                "io_ms": 80,
                            },
                        ],
                    },
                }
            ),
            "diagnostics_json": None,
        }
    )
    assert [leg.io_ms for leg in row.legs] == [20, 80]
    assert [leg.slot_wait_ms for leg in row.legs] == [5, 40]
    assert all(leg.timing_match == "native_id" for leg in row.legs)
    assert len(row.venue_timings) == 1
    assert row.venue_timings[0].venue == "polymarket"
    assert row.venue_timings[0].io_ms == 80
    assert row.venue_timings[0].slot_wait_ms == 40
    assert row.venue_timings[0].call_count == 2
    assert row.venue_timings[0].aggregation == "venue_max"


def test_trade_id_without_filled_cycle_is_linked_not_filled() -> None:
    row = project_audit_row(
        {
            "snapshot_id": "exec:linked",
            "opportunity_id": "watch:mkt-link",
            "occurred_at": PRICE2_END.isoformat(),
            "accepted": 1,
            "trade_id": "trade-partial-1",
            "cycle_outcome": "accepted",
            "snapshot_json": json.dumps(
                {
                    "started_at": PRICE2_START.isoformat(),
                    "evaluated_at": PRICE2_END.isoformat(),
                    "trade_id": "trade-partial-1",
                    "cycle_outcome": "accepted",
                }
            ),
            "diagnostics_json": None,
        }
    )
    assert row.filled is False
    assert row.trade_linked is True
    filled = project_audit_row(
        {
            "snapshot_id": "exec:filled",
            "opportunity_id": "watch:mkt-link",
            "occurred_at": PRICE2_END.isoformat(),
            "accepted": 1,
            "trade_id": "trade-filled-1",
            "cycle_outcome": "filled",
            "snapshot_json": "{}",
            "diagnostics_json": None,
        }
    )
    assert filled.filled is True
    assert filled.trade_linked is True


def test_quote_evaluation_elapsed_does_not_use_audit_or_assembly_fallback() -> None:
    later_audit = PRICE2_END + timedelta(seconds=3)
    row = project_audit_row(
        {
            "snapshot_id": "exec:no-eval",
            "opportunity_id": "watch:mkt-time",
            "occurred_at": later_audit.isoformat(),
            "accepted": 0,
            "snapshot_json": json.dumps(
                {
                    "started_at": PRICE2_START.isoformat(),
                    "timing": {"assembly_ms": 812},
                }
            ),
            "diagnostics_json": json.dumps({"assembly_ms": 812}),
        }
    )
    assert row.finished_at is None
    assert row.elapsed_ms is None
    assert row.occurred_at == later_audit
    complete = project_audit_row(
        {
            "snapshot_id": "exec:eval",
            "opportunity_id": "watch:mkt-time",
            "occurred_at": later_audit.isoformat(),
            "accepted": 1,
            "snapshot_json": json.dumps(
                {
                    "started_at": PRICE2_START.isoformat(),
                    "evaluated_at": PRICE2_END.isoformat(),
                    "timing": {"assembly_ms": 1},
                }
            ),
            "diagnostics_json": None,
        }
    )
    assert complete.elapsed_ms == 812
    assert complete.finished_at == PRICE2_END


def test_recent_window_includes_price2_without_price1_operator_event() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    client = _client(service)
    try:
        _observe(service, "mkt-standalone")
        for index in range(120):
            _record_audit(
                service,
                snapshot_id=f"exec:old-{index}",
                opportunity_id=f"watch:old-{index}",
                accepted=False,
                occurred_at=OBSERVED - timedelta(hours=2, seconds=index),
                market_id=f"old-{index}",
            )
        _record_audit(
            service,
            snapshot_id="exec:standalone",
            opportunity_id="watch:mkt-standalone",
            accepted=True,
            occurred_at=PRICE2_END,
            market_id="mkt-standalone",
        )
        rows = client.get(
            "/paper/watchlist/price2-attempts",
            params={
                "since": OBSERVED.isoformat(),
                "include_recent": True,
                "limit": 50,
            },
        ).json()
        assert any(item["snapshot_id"] == "exec:standalone" for item in rows)
        plan = repository._connection.execute(
            "EXPLAIN QUERY PLAN SELECT snapshot_id FROM execution_snapshot_audits "
            "WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT ?",
            (OBSERVED.isoformat(), 50),
        ).fetchall()
        plan_text = " ".join(" ".join(str(part) for part in row) for row in plan)
        assert "idx_execution_snapshot_audits_occurred" in plan_text
        assert "SCAN TABLE execution_snapshot_audits" not in plan_text
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_price2_projection_does_not_touch_capture_or_providers() -> None:
    source = inspect.getsource(capture_with_execution_reprice)
    assert "price2_activity" not in source
    assert "price2-attempts" not in source
    projection = inspect.getsource(project_audit_row)
    assert "reprice_for_paper_entry" not in projection
    assert "get_order_book" not in projection
