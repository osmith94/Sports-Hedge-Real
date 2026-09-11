from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.arbitrage import dislocations as dislocations_pkg
from sports_hedge.arbitrage.dislocations import (
    CanonicalEventContext,
    DislocationTracker,
    EventAnnotationInput,
    EventCategory,
    EventDrivenDislocationService,
    NearArbSignal,
    RateBudget,
    ScanCandidate,
    ScanPriority,
    VenueQuoteSnapshot,
    evaluate_candidate,
    event_category_from_label,
    schedule_scans,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName


EVENT_AT = datetime(2026, 9, 12, 15, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 12, 15, 12, 2, tzinfo=UTC)
PACKAGE_ROOT = Path(dislocations_pkg.__file__).parent


def _event(
    event_id: str = "e-1",
    market_id: str = "m-1",
    venues: int = 2,
) -> CanonicalEventContext:
    return CanonicalEventContext(
        canonical_event_id=event_id,
        canonical_market_id=market_id,
        as_of=AS_OF,
        kickoff_utc=datetime(2026, 9, 12, 15, 0, tzinfo=UTC),
        match_state="in_play",
        economically_equivalent_venues=venues,
    )


def _annotation(
    category: EventCategory = EventCategory.RED_CARD,
    event_id: str = "e-1",
) -> EventAnnotationInput:
    return EventAnnotationInput(
        canonical_event_id=event_id,
        category=category,
        occurred_at=EVENT_AT,
        retrieved_at=EVENT_AT + timedelta(seconds=1),
        source_timestamp=EVENT_AT,
        source="authorised_public_feed",
    )


def _quote(
    venue: VenueName,
    *,
    event_id: str = "e-1",
    market_id: str = "m-1",
    outcome: str = "home",
    odds: str = "2.10",
    quote_at: datetime | None = None,
    retrieved_at: datetime | None = None,
    suspended: bool = False,
    settlement: bool = True,
    costs: bool = True,
    depth: str = "800",
    liquidity: str = "4000",
    age_ms: int | None = 200,
) -> VenueQuoteSnapshot:
    quoted = quote_at or (EVENT_AT + timedelta(seconds=1))
    return VenueQuoteSnapshot(
        venue=venue,
        canonical_event_id=event_id,
        canonical_market_id=market_id,
        canonical_outcome=outcome,
        quote_timestamp=quoted,
        retrieved_at=retrieved_at or (quoted + timedelta(milliseconds=age_ms or 0)),
        source_timestamp=quoted,
        decimal_odds=Decimal(odds),
        executable_depth=Decimal(depth),
        liquidity=Decimal(liquidity),
        suspended=suspended,
        settlement_semantics_complete=settlement,
        costs_and_fx_complete=costs,
        quote_age_ms=age_ms,
    )


def _fresh_pair(
    event_id: str = "e-1",
    *,
    left_odds: str = "2.00",
    right_odds: str = "2.40",
    depth: str = "800",
    liquidity: str = "4000",
) -> list[VenueQuoteSnapshot]:
    return [
        _quote(
            VenueName.MATCHBOOK,
            event_id=event_id,
            odds=left_odds,
            depth=depth,
            liquidity=liquidity,
        ),
        _quote(
            VenueName.POLYMARKET,
            event_id=event_id,
            odds=right_odds,
            depth=depth,
            liquidity=liquidity,
        ),
    ]


def test_red_card_raises_scan_priority_for_affected_canonical_event() -> None:
    baseline = evaluate_candidate(
        ScanCandidate(event=_event(), quotes=[], execution_risk_score=50)
    )
    after = evaluate_candidate(
        ScanCandidate(
            event=_event(),
            quotes=[],
            annotation=_annotation(EventCategory.RED_CARD),
            execution_risk_score=50,
        )
    )
    assert baseline.priority == ScanPriority.NORMAL
    assert after.priority in {ScanPriority.ELEVATED, ScanPriority.BURST, ScanPriority.CRITICAL}
    assert after.priority_rank > baseline.priority_rank
    assert after.canonical_event_id == "e-1"
    assert "event:RED_CARD" in after.reasons


def test_suspended_venue_quote_cannot_create_executable_dislocation() -> None:
    decision = evaluate_candidate(
        ScanCandidate(
            event=_event(),
            annotation=_annotation(),
            quotes=[
                _quote(VenueName.MATCHBOOK, odds="2.00", suspended=True),
                _quote(VenueName.POLYMARKET, odds="2.50"),
            ],
            market_liquidity=Decimal("8000"),
        )
    )
    assert decision.executable_dislocation is False
    assert any("venue_suspended" in item.reasons for item in decision.quote_eligibility)
    assert "venue_suspended" in decision.reasons


def test_stale_pre_event_quote_is_rejected_after_material_event() -> None:
    stale = _quote(
        VenueName.MATCHBOOK,
        odds="2.00",
        quote_at=EVENT_AT - timedelta(seconds=30),
        age_ms=200,
    )
    fresh = _quote(VenueName.POLYMARKET, odds="2.50")
    decision = evaluate_candidate(
        ScanCandidate(event=_event(), annotation=_annotation(), quotes=[stale, fresh])
    )
    matchbook = next(
        item for item in decision.quote_eligibility if item.venue == VenueName.MATCHBOOK
    )
    assert matchbook.executable is False
    assert "stale_pre_event_quote" in matchbook.reasons
    assert decision.executable_dislocation is False
    assert matchbook.quote_timestamp < EVENT_AT
    assert decision.event_occurred_at == EVENT_AT


def test_two_fresh_venues_with_material_dispersion_enter_burst_priority() -> None:
    decision = evaluate_candidate(
        ScanCandidate(
            event=_event(),
            quotes=_fresh_pair(left_odds="1.90", right_odds="2.60"),
            market_liquidity=Decimal("3000"),
            execution_risk_score=45,
        )
    )
    assert decision.fresh_executable_venues == 2
    assert decision.cross_venue_dispersion >= Decimal("0.03")
    assert decision.priority in {ScanPriority.BURST, ScanPriority.CRITICAL}
    assert decision.executable_dislocation is True


def test_twenty_simultaneous_games_under_constrained_budget_are_ranked_deterministically() -> None:
    candidates = [
        ScanCandidate(
            event=_event(event_id=f"e-{index:02d}"),
            quotes=_fresh_pair(
                event_id=f"e-{index:02d}",
                left_odds="2.05",
                right_odds="2.10" if index < 15 else "2.55",
                depth=str(100 + index),
                liquidity=str(500 + index * 10),
            ),
            annotation=_annotation(EventCategory.GOAL, event_id=f"e-{index:02d}")
            if index >= 15
            else None,
            near_arb=NearArbSignal(
                distance_to_trigger=Decimal("0.020") - Decimal(index) * Decimal("0.0005")
            ),
            market_liquidity=Decimal(500 + index * 10),
            execution_risk_score=50,
            headline_move=Decimal("0.20") if index < 5 else Decimal("0.01"),
        )
        for index in range(20)
    ]
    budget = RateBudget(max_scans=7)
    first = schedule_scans(candidates, budget)
    second = schedule_scans(list(reversed(candidates)), budget)
    assert first.selected_count == 7
    assert len(first.deferred) == 13
    first_ids = [item.decision.canonical_event_id for item in first.selected]
    second_ids = [item.decision.canonical_event_id for item in second.selected]
    assert first_ids == second_ids
    assert all(item.budget_reason == "within_rate_budget" for item in first.selected)
    assert all(item.budget_reason == "rate_budget_exhausted" for item in first.deferred)


def test_high_liquidity_close_to_arb_outranks_low_liquidity_noise() -> None:
    valuable = ScanCandidate(
        event=_event(event_id="e-liquid"),
        quotes=_fresh_pair(
            event_id="e-liquid",
            left_odds="2.05",
            right_odds="2.12",
            depth="2000",
            liquidity="9000",
        ),
        near_arb=NearArbSignal(distance_to_trigger=Decimal("0.002")),
        market_liquidity=Decimal("9000"),
        execution_risk_score=25,
        headline_move=Decimal("0.01"),
    )
    noise = ScanCandidate(
        event=_event(event_id="e-noise"),
        quotes=_fresh_pair(
            event_id="e-noise",
            left_odds="1.80",
            right_odds="3.40",
            depth="20",
            liquidity="40",
        ),
        near_arb=NearArbSignal(distance_to_trigger=Decimal("0.040")),
        market_liquidity=Decimal("40"),
        execution_risk_score=80,
        headline_move=Decimal("0.25"),
    )
    plan = schedule_scans([noise, valuable], RateBudget(max_scans=1))
    assert plan.selected_count == 1
    assert plan.selected[0].decision.canonical_event_id == "e-liquid"
    assert plan.deferred[0].decision.canonical_event_id == "e-noise"
    assert plan.selected[0].decision.composite_score > plan.deferred[0].decision.composite_score


def test_event_timestamp_and_quote_timestamp_remain_separate() -> None:
    quote = _quote(VenueName.MATCHBOOK, quote_at=EVENT_AT + timedelta(seconds=4), age_ms=150)
    candidate = ScanCandidate(event=_event(), annotation=_annotation(), quotes=[quote])
    decision = evaluate_candidate(candidate)
    assert candidate.annotation is not None
    assert candidate.annotation.occurred_at != quote.quote_timestamp
    assert candidate.annotation.retrieved_at != quote.retrieved_at
    assert decision.quote_eligibility[0].quote_timestamp == quote.quote_timestamp
    assert decision.event_occurred_at == EVENT_AT
    assert "event_timestamp_separated_from_quote_timestamp" in decision.reasons


def test_rate_budget_limit_is_respected() -> None:
    candidates = [
        ScanCandidate(event=_event(event_id=f"e-{i}"), quotes=_fresh_pair(event_id=f"e-{i}"))
        for i in range(5)
    ]
    plan = schedule_scans(candidates, RateBudget(max_scans=2, remaining_scans=1))
    assert plan.budget_limit == 1
    assert plan.selected_count == 1
    assert len(plan.deferred) == 4
    empty = schedule_scans(candidates, RateBudget(max_scans=0))
    assert empty.selected_count == 0
    assert len(empty.deferred) == 5


def test_no_execution_side_effects() -> None:
    forbidden = ("place_order", "cancel_order", "place_bet", "sign_wallet", "submit_order")
    sources = []
    for path in PACKAGE_ROOT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        sources.append(text)
        for token in forbidden:
            assert f"def {token}" not in text, f"{token} found in {path}"
    joined = "\n".join(sources)
    assert "paper_mode" in joined
    assert "authorised_public_feeds_only" in joined
    candidate = ScanCandidate(
        event=_event(),
        quotes=_fresh_pair(),
        annotation=_annotation(),
        market_liquidity=Decimal("4000"),
    )
    original = candidate.model_copy(deep=True)
    service = EventDrivenDislocationService(settings=Settings())
    decision = service.evaluate(candidate)
    observed = service.observe(candidate)
    assert candidate == original
    assert decision.paper_mode is True
    assert observed.paper_mode is True
    assert observed.causality_claim is None
    for name in forbidden:
        assert not hasattr(service, name)
        assert not hasattr(service.tracker, name)


def test_incomplete_settlement_and_missing_costs_fail_closed() -> None:
    quotes = [
        _quote(VenueName.MATCHBOOK, odds="2.00", settlement=False),
        _quote(VenueName.POLYMARKET, odds="2.50", costs=False),
    ]
    decision = evaluate_candidate(ScanCandidate(event=_event(), quotes=quotes))
    assert decision.executable_dislocation is False
    assert "incomplete_settlement_semantics" in decision.reasons
    assert "missing_costs_or_fx" in decision.reasons


def test_tracker_records_suspension_reopen_and_does_not_claim_causality() -> None:
    tracker = DislocationTracker()
    annotation = _annotation()
    first = tracker.observe(
        canonical_event_id="e-1",
        canonical_market_id="m-1",
        as_of=EVENT_AT + timedelta(seconds=1),
        quotes=[
            _quote(VenueName.MATCHBOOK, suspended=True, odds="2.00"),
            _quote(VenueName.POLYMARKET, odds="2.10"),
        ],
        annotation=annotation,
    )
    assert first.venues["matchbook"].suspended is True
    assert first.venues["matchbook"].suspended_at is not None
    second = tracker.observe(
        canonical_event_id="e-1",
        canonical_market_id="m-1",
        as_of=EVENT_AT + timedelta(seconds=8),
        quotes=[
            _quote(VenueName.MATCHBOOK, odds="1.70", quote_at=EVENT_AT + timedelta(seconds=8)),
            _quote(VenueName.POLYMARKET, odds="2.40", quote_at=EVENT_AT + timedelta(seconds=8)),
        ],
        annotation=annotation,
        near_arb=NearArbSignal(
            distance_to_trigger=Decimal("0.004"),
            near_arb_active=True,
            near_arb_duration_seconds=6.0,
            validated_arb_active=True,
            validated_arb_duration_seconds=1.5,
        ),
    )
    assert second.venues["matchbook"].suspended is False
    assert second.venues["matchbook"].reopened_at is not None
    assert second.venues["matchbook"].first_fresh_post_event_quote_at is not None
    assert second.dispersion_peak is not None
    assert second.near_arb_duration_seconds == 6.0
    assert second.validated_arb_duration_seconds == 1.5
    assert second.causality_claim is None
    assert second.event_occurred_at != second.venues["matchbook"].last_quote_timestamp


def test_unknown_event_label_fails_closed() -> None:
    assert event_category_from_label("RED_CARD") == EventCategory.RED_CARD
    assert event_category_from_label("red_card") == EventCategory.RED_CARD
    assert event_category_from_label("rumour_from_unauthorised_source") is None


def test_package_stays_isolated_from_collector_event_ingestion_and_venues() -> None:
    forbidden_imports = (
        "sports_hedge.application.collector",
        "sports_hedge.market_intelligence",
        "sports_hedge.venues",
        "sports_hedge.application.paper_scan",
    )
    for path in PACKAGE_ROOT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for module in forbidden_imports:
            assert module not in text, f"{path} imports {module}"


def test_dislocation_api_is_read_only_paper_model() -> None:
    client = TestClient(app)
    payload = ScanCandidate(
        event=_event(),
        annotation=_annotation(),
        quotes=_fresh_pair(left_odds="1.95", right_odds="2.55"),
        market_liquidity=Decimal("6000"),
        execution_risk_score=40,
    ).model_dump(mode="json")
    response = client.post("/arbitrage/dislocations/evaluate", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["paper_mode"] is True
    assert body["data_class"] == "modelled"
    assert body["priority"] in {"ELEVATED", "BURST", "CRITICAL"}
    schedule = client.post(
        "/arbitrage/dislocations/schedule",
        json={"candidates": [payload], "budget": {"max_scans": 1}},
    )
    assert schedule.status_code == 200
    assert schedule.json()["selected_count"] == 1
    observed = client.post("/arbitrage/dislocations/observe", json=payload)
    assert observed.status_code == 200
    assert observed.json()["causality_claim"] is None
