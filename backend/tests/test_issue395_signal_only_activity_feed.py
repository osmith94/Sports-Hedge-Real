"""Issue #395: primary Activity feed is operator-signal only.

Noise lifecycle events stay append-only in SQLite and unfiltered /activity.
PAPER / read-only. No venue writes, extra provider calls, or capture-path changes.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import bind_price_engine_item_persist
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.price_engine import (
    CataloguePriceEngine,
    HotPromotionFact,
    PriceEnginePriority,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.arbitrage.watchlist.models import (
    OPERATOR_ACTIVITY_EVENT_TYPES,
    LifecycleEventType,
    OpportunityStatus,
    PaperFillAttempt,
    PaperFillAttemptStatus,
    WatchLeg,
    WatchObservation,
    hot_promotion_lifecycle_event_id,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision
from test_issue344_price_engine import (
    DISTANT_KICKOFF,
    NEAR_KICKOFF,
    NOW,
    StubPaperScan,
    _engine,
    _qualifying_decision,
    _row,
)

TRIGGER = Decimal("0.01")
EDGE_080 = Decimal("0.008")
EDGE_120 = Decimal("0.012")
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def _legs() -> list[WatchLeg]:
    return [
        WatchLeg(
            outcome="yes",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-1",
            currency="GBP",
            native_stake=Decimal("50"),
            gbp_per_unit=Decimal("1"),
            gbp_stake=Decimal("50"),
            net_decimal_odds=Decimal("2.10"),
            cumulative_depth_gbp=Decimal("80"),
        ),
        WatchLeg(
            outcome="no",
            venue=VenueName.KALSHI,
            source_market_id="ks-1",
            currency="USD",
            native_stake=Decimal("40") / Decimal("0.75"),
            gbp_per_unit=Decimal("0.75"),
            gbp_stake=Decimal("40"),
            net_decimal_odds=Decimal("2.05"),
            cumulative_depth_gbp=Decimal("60"),
        ),
    ]


def _observation(
    *,
    market_id: str = "mkt-signal",
    edge: Decimal | None = EDGE_080,
    observed_at: datetime = OBSERVED,
    eligible: bool = False,
    rejection_reasons: list[str] | None = None,
) -> WatchObservation:
    implied = None if edge is None else Decimal("1") / (Decimal("1") + edge)
    return WatchObservation(
        observed_at=observed_at,
        canonical_event_id="evt-signal",
        canonical_market_id=market_id,
        settlement_key="regulation_time|full_time",
        competition="Premier League",
        home_team="Brentford",
        away_team="Chelsea",
        market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        period=FootballPeriod.FULL_TIME,
        legs=_legs(),
        trigger_net_edge=TRIGGER,
        current_net_edge=edge,
        implied_probability_sum=implied,
        solver_is_arbitrage=True,
        eligible_for_paper_simulation=eligible,
        rejection_reasons=list(rejection_reasons or []),
        quote_age_ms=120,
        limiting_depth_gbp=Decimal("60"),
        capital_required_gbp=Decimal("90"),
        guaranteed_profit_gbp=Decimal("1.10") if eligible else None,
    )


def _uninteresting_decision(*, scanned_at: datetime = NOW) -> PaperScanDecision:
    return PaperScanDecision(
        scanned_at=scanned_at,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["register"]),
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(
                is_arbitrage=False,
                roi=Decimal("0.001"),
                minimum_state_pnl=Decimal("-0.01"),
                numerically_validated=True,
            )
        ),
        minimum_net_edge=Decimal("0.01"),
        solver_model="strict_complete_set",
        eligible_for_paper_simulation=False,
    )


def test_noise_lifecycle_events_stay_persisted_but_leave_operator_feed() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(
        _observation(rejection_reasons=["net_edge_below_threshold"], edge=EDGE_080)
    )
    service.observe(
        _observation(
            rejection_reasons=["net_edge_below_threshold"],
            edge=Decimal("0.009"),
            observed_at=OBSERVED + timedelta(seconds=5),
        )
    )
    service.observe(
        _observation(
            rejection_reasons=["net_edge_below_threshold"],
            edge=Decimal("0.007"),
            observed_at=OBSERVED + timedelta(seconds=10),
        )
    )
    service.observe(
        _observation(
            rejection_reasons=["market_not_equivalent"],
            observed_at=OBSERVED + timedelta(seconds=15),
        )
    )
    persisted = {event.event_type for event in service.activity(opportunity_id=first.opportunity_id)}
    assert LifecycleEventType.CANDIDATE_FIRST_SEEN in persisted
    assert LifecycleEventType.MOVED_CLOSER_TO_TRIGGER in persisted
    assert LifecycleEventType.MOVED_FURTHER_FROM_TRIGGER in persisted
    assert LifecycleEventType.REJECTED_SEMANTICS in persisted
    operator = {event.event_type for event in service.operator_activity()}
    assert operator.isdisjoint(
        {
            LifecycleEventType.CANDIDATE_FIRST_SEEN,
            LifecycleEventType.MOVED_CLOSER_TO_TRIGGER,
            LifecycleEventType.MOVED_FURTHER_FROM_TRIGGER,
            LifecycleEventType.REJECTED_SEMANTICS,
            LifecycleEventType.TRIGGER_CROSSED,
            LifecycleEventType.PAPER_FILL_ATTEMPTED,
        }
    )


def test_trigger_lost_before_fill_stays_on_operator_feed() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    triggered = service.observe(
        _observation(
            edge=EDGE_120,
            eligible=True,
            observed_at=OBSERVED,
        )
    )
    service.observe(
        _observation(
            edge=EDGE_080,
            rejection_reasons=["net_edge_below_threshold"],
            observed_at=OBSERVED + timedelta(seconds=8),
        )
    )
    types = [event.event_type for event in service.operator_activity()]
    assert types == [
        LifecycleEventType.TRIGGER_LOST_BEFORE_FILL,
        LifecycleEventType.PAPER_ELIGIBLE,
        LifecycleEventType.QUALIFYING_DETECTED,
    ]
    lost = service.operator_activity()[0]
    assert lost.fixture_label == "Brentford v Chelsea"
    assert lost.market_family == "both_teams_to_score"
    assert lost.capture_eligible is True
    audit = [event.event_type for event in service.activity(opportunity_id=triggered.opportunity_id)]
    assert LifecycleEventType.TRIGGER_LOST_BEFORE_FILL in audit
    assert LifecycleEventType.CANDIDATE_FIRST_SEEN in audit
    assert LifecycleEventType.TRIGGER_CROSSED in audit


def test_completed_paper_entry_renders_one_trade_entered_and_closed_renders_exited() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    triggered = service.observe(
        _observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED)
    )
    service.record_paper_fill(
        triggered.opportunity_id,
        stage=OpportunityStatus.PAPER_FILLING,
        occurred_at=OBSERVED + timedelta(seconds=1),
        detail="paper_fill_attempted_bound_snapshot",
    )
    filled = service.record_paper_fill(
        triggered.opportunity_id,
        stage=OpportunityStatus.FILLED,
        occurred_at=OBSERVED + timedelta(seconds=2),
        detail="paper_mode_only",
    )
    assert filled.status is OpportunityStatus.FILLED
    service.close(
        triggered.opportunity_id,
        occurred_at=OBSERVED + timedelta(seconds=3),
        detail="paper settlement",
    )
    operator = service.operator_activity()
    assert [event.event_type for event in operator] == [
        LifecycleEventType.CLOSED,
        LifecycleEventType.PAPER_FILL_COMPLETE,
        LifecycleEventType.PAPER_ELIGIBLE,
        LifecycleEventType.QUALIFYING_DETECTED,
    ]
    assert all(event.fixture_label == "Brentford v Chelsea" for event in operator)
    assert all(event.market_family == "both_teams_to_score" for event in operator)
    complete = [
        event
        for event in service.activity(opportunity_id=triggered.opportunity_id)
        if event.event_type is LifecycleEventType.PAPER_FILL_COMPLETE
    ]
    assert len(complete) == 1
    audit_types = {
        event.event_type for event in service.activity(opportunity_id=triggered.opportunity_id)
    }
    assert LifecycleEventType.PAPER_FILL_ATTEMPTED in audit_types
    assert LifecycleEventType.PAPER_FILL_ATTEMPTED not in {event.event_type for event in operator}


def test_operator_activity_keeps_newest_first_chronology() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    service.observe(
        _observation(
            edge=EDGE_080,
            rejection_reasons=["net_edge_below_threshold"],
            observed_at=OBSERVED + timedelta(seconds=5),
        )
    )
    other = service.observe(
        _observation(
            market_id="mkt-later",
            edge=EDGE_120,
            eligible=True,
            observed_at=OBSERVED + timedelta(seconds=10),
        )
    )
    service.record_paper_fill(
        other.opportunity_id,
        stage=OpportunityStatus.PAPER_FILLING,
        occurred_at=OBSERVED + timedelta(seconds=11),
    )
    service.record_paper_fill(
        other.opportunity_id,
        stage=OpportunityStatus.FILLED,
        occurred_at=OBSERVED + timedelta(seconds=12),
    )
    operator = service.operator_activity()
    assert [event.event_type for event in operator] == [
        LifecycleEventType.PAPER_FILL_COMPLETE,
        LifecycleEventType.PAPER_ELIGIBLE,
        LifecycleEventType.QUALIFYING_DETECTED,
        LifecycleEventType.TRIGGER_LOST_BEFORE_FILL,
        LifecycleEventType.PAPER_ELIGIBLE,
        LifecycleEventType.QUALIFYING_DETECTED,
    ]
    assert operator[0].occurred_at > operator[1].occurred_at
    assert operator[1].opportunity_id == other.opportunity_id
    assert operator[2].opportunity_id == other.opportunity_id
    assert operator[3].opportunity_id == first.opportunity_id
    assert operator[4].opportunity_id == first.opportunity_id
    assert first.opportunity_id != other.opportunity_id


def test_hot_promotion_is_idempotent_per_episode() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.record_hot_promotion(
        canonical_event_id="evt-hot",
        occurred_at=OBSERVED,
        episode=1,
        fixture_label="Brentford v Chelsea",
        market_family="both_teams_to_score",
        pricing_lane="background",
        current_net_edge=EDGE_080,
        distance_to_trigger_pp=Decimal("0.2000"),
    )
    again = service.record_hot_promotion(
        canonical_event_id="evt-hot",
        occurred_at=OBSERVED + timedelta(seconds=1),
        episode=1,
        fixture_label="Brentford v Chelsea",
        market_family="both_teams_to_score",
        pricing_lane="background",
        current_net_edge=EDGE_080,
        distance_to_trigger_pp=Decimal("0.2000"),
    )
    assert first.event_id == again.event_id
    assert first.event_id == hot_promotion_lifecycle_event_id("evt-hot", 1)
    events = service.operator_activity()
    assert len(events) == 1
    assert events[0].event_type is LifecycleEventType.PROMOTED_TO_HOT
    assert "Brentford v Chelsea" in (events[0].detail or "")
    assert "BACKGROUND → HOT" in (events[0].detail or "")
    assert events[0].fixture_label == "Brentford v Chelsea"
    assert events[0].market_family == "both_teams_to_score"
    assert events[0].canonical_event_id == "evt-hot"


def test_watchlist_activity_api_operator_signal_hides_noise() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        service.observe(_observation(rejection_reasons=["net_edge_below_threshold"]))
        service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED + timedelta(seconds=2)))
        service.observe(
            _observation(
                edge=EDGE_080,
                rejection_reasons=["net_edge_below_threshold"],
                observed_at=OBSERVED + timedelta(seconds=4),
            )
        )
        audit = client.get("/paper/watchlist/activity", params={"limit": 50})
        assert audit.status_code == 200
        audit_types = {item["event_type"] for item in audit.json()}
        assert "candidate_first_seen" in audit_types
        assert "trigger_lost_before_fill" in audit_types
        operator = client.get(
            "/paper/watchlist/activity",
            params={"limit": 50, "operator_signal": True},
        )
        assert operator.status_code == 200
        operator_types = [item["event_type"] for item in operator.json()]
        assert operator_types == [
            "trigger_lost_before_fill",
            "paper_eligible",
            "qualifying_detected",
        ]
        payload = operator.json()[0]
        assert payload["fixture_label"] == "Brentford v Chelsea"
        assert payload["market_family"] == "both_teams_to_score"
        assert payload["capture_eligible"] is True
        assert OPERATOR_ACTIVITY_EVENT_TYPES == {
            LifecycleEventType.PROMOTED_TO_HOT,
            LifecycleEventType.QUALIFYING_DETECTED,
            LifecycleEventType.QUALIFYING_LOST,
            LifecycleEventType.PAPER_ELIGIBLE,
            LifecycleEventType.TRIGGER_LOST_BEFORE_FILL,
            LifecycleEventType.PAPER_FILL_COMPLETE,
            LifecycleEventType.CLOSED,
        }
    finally:
        app.dependency_overrides.clear()
        repository.close()


@pytest.mark.asyncio
async def test_price_engine_emits_one_hot_promotion_from_background_not_on_refresh() -> None:
    facts: list[HotPromotionFact] = []
    paper = StubPaperScan(_qualifying_decision(scanned_at=NOW))
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="cover", kickoff=DISTANT_KICKOFF, matchbook_market_id="316131", kalshi_event="KXEPLBTTS-COVER")],
        paper_scan=paper,
        on_hot_promotion=facts.append,
    )
    runtime = engine.item("amc-cover")
    assert runtime is not None
    assert runtime.priority is PriceEnginePriority.BACKGROUND
    first = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert runtime.identity.canonical_event_id in first.promotions
    assert [fact.canonical_event_id for fact in facts] == [runtime.identity.canonical_event_id]
    assert facts[0].episode == 1
    assert facts[0].pricing_lane == PriceEnginePriority.BACKGROUND.value
    assert engine.classify_priority(runtime.identity) is PriceEnginePriority.HOT

    second = await engine.run_slice(PriceEnginePriority.HOT, now=NOW + timedelta(seconds=5))
    assert runtime.identity.canonical_event_id not in second.promotions
    assert len(facts) == 1


@pytest.mark.asyncio
async def test_already_hot_lifecycle_refresh_does_not_emit_operator_promotion() -> None:
    facts: list[HotPromotionFact] = []
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="hot", kickoff=NEAR_KICKOFF, matchbook_market_id="316121", kalshi_event="KXEPLBTTS-HOT")],
        paper_scan=StubPaperScan(_qualifying_decision(scanned_at=NOW)),
        on_hot_promotion=facts.append,
        hot_interval=0,
    )
    runtime = engine.item("amc-hot")
    assert runtime is not None
    assert runtime.priority is PriceEnginePriority.HOT
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    assert "amc-hot" in result.evaluated
    assert facts == []


@pytest.mark.asyncio
async def test_demotion_then_repromotion_is_a_new_hot_episode() -> None:
    facts: list[HotPromotionFact] = []
    paper = StubPaperScan(_qualifying_decision(scanned_at=NOW))
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="cover", kickoff=DISTANT_KICKOFF, matchbook_market_id="316131", kalshi_event="KXEPLBTTS-COVER")],
        paper_scan=paper,
        on_hot_promotion=facts.append,
    )
    await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    paper.decision = _uninteresting_decision(scanned_at=NOW + timedelta(seconds=5))
    await engine.run_slice(PriceEnginePriority.HOT, now=NOW + timedelta(seconds=5))
    runtime = engine.item("amc-cover")
    assert runtime is not None
    assert runtime.identity.canonical_event_id not in engine._promoted_hot_ids
    paper.decision = _qualifying_decision(scanned_at=NOW + timedelta(seconds=10))
    await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW + timedelta(seconds=10))
    assert [fact.episode for fact in facts] == [1, 2]


def test_bind_persist_wires_price_engine_hot_promotion_without_capture_ownership() -> None:
    src = inspect.getsource(bind_price_engine_item_persist)
    assert "on_hot_promotion" in src
    assert "record_hot_promotion" in src
    assert "persist_triggered_chain" not in src
    promote_src = inspect.getsource(CataloguePriceEngine._maybe_promote)
    assert "_emit_operator_hot_promotion" in promote_src
    assert "moved_closer_to_trigger" not in promote_src


def test_hot_promotion_persist_is_observational_and_cannot_drop_fixture_state() -> None:
    emit_src = inspect.getsource(CataloguePriceEngine._emit_operator_hot_promotion)
    persist_src = inspect.getsource(WatchlistService.record_hot_promotion)
    bind_src = inspect.getsource(bind_price_engine_item_persist)
    for src in (emit_src, persist_src, bind_src):
        assert "_drop_identity" not in src
        assert "upsert_from_report" not in src
        assert "fixture_current_state" not in src
        assert "hot_identity_scope" not in src
    maybe_src = inspect.getsource(CataloguePriceEngine._maybe_promote)
    assert maybe_src.index("_promoted_hot_ids.add") < maybe_src.index(
        "_emit_operator_hot_promotion"
    )

    now = datetime.now(UTC)
    kickoff = now + timedelta(days=3)
    store = FixtureCurrentStateStore()
    fixture = DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id="1001",
        canonical_event_id="evt-radar",
        home_team="Brentford",
        away_team="Chelsea",
        competition="Premier League",
        kickoff_utc=kickoff,
        last_seen_at=now,
        matchbook_matched=True,
        market_evaluation_state="evaluated",
        opportunity_state="unmatched",
    )
    store.upsert_from_report(
        CollectionReport(started_at=now, completed_at=now, discovered_fixtures=[fixture]),
        scan_lane=ScanLane.UNIVERSE,
        now=now,
    )
    before = [row.canonical_event_id for row in store.inventory(now)]
    assert before == ["evt-radar"]
    service = WatchlistService(SqliteWatchlistRepository())
    service.record_hot_promotion(
        canonical_event_id="evt-radar",
        occurred_at=now,
        episode=1,
        fixture_label="Brentford v Chelsea",
        market_family="both_teams_to_score",
    )
    after = [row.canonical_event_id for row in store.inventory(now)]
    assert after == before
    assert [event.event_type for event in service.operator_activity()] == [
        LifecycleEventType.PROMOTED_TO_HOT
    ]


def test_economic_only_trigger_lost_stays_in_audit_not_operator_feed() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    triggered = service.observe(_observation(edge=EDGE_120, eligible=False, observed_at=OBSERVED))
    assert triggered.status is OpportunityStatus.TRIGGERED
    assert triggered.capture_eligible is False
    service.observe(
        _observation(
            edge=EDGE_080,
            rejection_reasons=["net_edge_below_threshold"],
            observed_at=OBSERVED + timedelta(seconds=8),
        )
    )
    audit = [
        event
        for event in service.activity(opportunity_id=triggered.opportunity_id)
        if event.event_type is LifecycleEventType.TRIGGER_LOST_BEFORE_FILL
    ]
    assert len(audit) == 1
    assert audit[0].capture_eligible is False
    assert audit[0].fixture_label == "Brentford v Chelsea"
    assert LifecycleEventType.TRIGGER_CROSSED in {
        event.event_type for event in service.activity(opportunity_id=triggered.opportunity_id)
    }
    operator_types = [event.event_type for event in service.operator_activity()]
    assert operator_types == [
        LifecycleEventType.QUALIFYING_LOST,
        LifecycleEventType.QUALIFYING_DETECTED,
    ]
    assert LifecycleEventType.TRIGGER_LOST_BEFORE_FILL not in operator_types
    assert LifecycleEventType.PAPER_ELIGIBLE not in operator_types


def test_sticky_capture_eligible_during_triggered_episode_is_operator_visible() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(_observation(edge=EDGE_120, eligible=False, observed_at=OBSERVED))
    assert first.capture_eligible is False
    second = service.observe(
        _observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED + timedelta(seconds=2))
    )
    assert second.status is OpportunityStatus.TRIGGERED
    assert second.capture_eligible is True
    service.observe(
        _observation(
            edge=EDGE_080,
            rejection_reasons=["net_edge_below_threshold"],
            observed_at=OBSERVED + timedelta(seconds=6),
        )
    )
    operator = service.operator_activity()
    assert [event.event_type for event in operator] == [
        LifecycleEventType.TRIGGER_LOST_BEFORE_FILL,
        LifecycleEventType.PAPER_ELIGIBLE,
        LifecycleEventType.QUALIFYING_DETECTED,
    ]
    assert operator[0].capture_eligible is True
    assert operator[1].capture_eligible is True
    assert operator[1].canonical_event_id == "evt-signal"
    assert operator[1].canonical_market_id == "mkt-signal"


def test_durable_fill_attempt_start_suppresses_trigger_lost() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    triggered = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    service.repository.upsert_paper_fill_attempt(
        PaperFillAttempt(
            attempt_id="attempt-unbound",
            opportunity_id=triggered.opportunity_id,
            bound_snapshot=False,
            status=PaperFillAttemptStatus.STARTED,
            started_at=OBSERVED + timedelta(seconds=1),
        )
    )
    service.observe(
        _observation(
            edge=EDGE_080,
            rejection_reasons=["net_edge_below_threshold"],
            observed_at=OBSERVED + timedelta(seconds=8),
        )
    )
    types = {event.event_type for event in service.activity(opportunity_id=triggered.opportunity_id)}
    assert LifecycleEventType.TRIGGER_LOST_BEFORE_FILL not in types
    assert [event.event_type for event in service.operator_activity()] == [
        LifecycleEventType.PAPER_ELIGIBLE,
        LifecycleEventType.QUALIFYING_DETECTED,
    ]


def test_operator_cards_carry_fixture_and_market_and_hide_trigger_crossed() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    triggered = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    assert triggered.status is OpportunityStatus.TRIGGERED
    service.record_hot_promotion(
        canonical_event_id="evt-signal",
        occurred_at=OBSERVED + timedelta(seconds=1),
        episode=1,
        fixture_label="Brentford v Chelsea",
        market_family="both_teams_to_score",
    )
    operator = service.operator_activity()
    assert {event.event_type for event in operator} == {
        LifecycleEventType.PROMOTED_TO_HOT,
        LifecycleEventType.QUALIFYING_DETECTED,
        LifecycleEventType.PAPER_ELIGIBLE,
    }
    assert all(event.fixture_label == "Brentford v Chelsea" for event in operator)
    assert all(event.market_family == "both_teams_to_score" for event in operator)
    crossed = [
        event
        for event in service.activity(opportunity_id=triggered.opportunity_id)
        if event.event_type is LifecycleEventType.TRIGGER_CROSSED
    ]
    assert crossed
    assert crossed[0].fixture_label == "Brentford v Chelsea"
    assert LifecycleEventType.TRIGGER_CROSSED not in {event.event_type for event in operator}
