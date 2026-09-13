from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import FeeScope
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.risk.execution import ExecutionRiskInputs
from venue_cost_helpers import matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def matchbook_payloads() -> tuple[dict, dict]:
    event = {
        "id": 1001,
        "name": "Newcastle United vs Chelsea",
        "start": KICKOFF.isoformat(),
        "competition-name": "Premier League",
    }
    market = {
        "id": 2001,
        "name": "Both Teams To Score",
        "runners": [
            {
                "id": 301,
                "name": "Yes",
                "prices": [
                    {"side": "back", "odds": "2.20", "available-amount": "40"},
                    {"side": "back", "odds": "2.10", "available-amount": "80"},
                    {"side": "lay", "odds": "2.22", "available-amount": "120"},
                ],
            },
            {
                "id": 302,
                "name": "No",
                "prices": [
                    {"side": "back", "odds": "1.80", "available-amount": "100"},
                    {"side": "lay", "odds": "1.82", "available-amount": "100"},
                ],
            },
        ],
    }
    return event, market


def polymarket_payloads() -> tuple[dict, dict, dict[str, dict]]:
    event = {
        "id": "pm-event-1",
        "title": "Newcastle United vs Chelsea",
        "startTime": KICKOFF.isoformat(),
        "competition": "Premier League",
    }
    market = {
        "id": "pm-market-1",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "description": "Resolves based on 90 minutes of regulation time.",
    }
    books = {
        "yes-token": {
            "asset_id": "yes-token",
            "bids": [{"price": "0.49", "size": "250"}],
            "asks": [{"price": "0.51", "size": "250"}],
        },
        "no-token": {
            "asset_id": "no-token",
            "bids": [{"price": "0.41", "size": "300"}],
            "asks": [
                {"price": "0.43", "size": "160"},
                {"price": "0.45", "size": "180"},
            ],
        },
    }
    return event, market, books


def test_source_rule_version_does_not_break_economic_market_equivalence() -> None:
    def event(venue: VenueName) -> CanonicalEvent:
        return CanonicalEvent(
            competition="Premier League",
            home_team="Newcastle United",
            away_team="Chelsea",
            kickoff_utc=KICKOFF,
            source_venue=venue,
            source_event_id=venue.value,
        )

    def market(venue: VenueName, rule_version: str | None) -> CanonicalMarket:
        return CanonicalMarket(
            event=event(venue),
            source_venue=venue,
            source_market_id=f"{venue.value}-btts",
            family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            period=FootballPeriod.FULL_TIME,
            settlement=SettlementFingerprint(
                scope=SettlementScope.REGULATION_TIME,
                period=FootballPeriod.FULL_TIME,
                extra_time_included=False,
                penalties_included=False,
                source_rule_version=rule_version,
            ),
            runners=[
                CanonicalRunner(source_runner_id="y", outcome=CanonicalOutcome.YES, label="Yes"),
                CanonicalRunner(source_runner_id="n", outcome=CanonicalOutcome.NO, label="No"),
            ],
        )

    result = MarketMatcher().match(
        market(VenueName.MATCHBOOK, None),
        market(VenueName.POLYMARKET, "pm-market-1"),
    )
    assert result.matched is True


def test_venue_builders_convert_native_books_to_common_decimal_depth() -> None:
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()

    matchbook = MatchbookObservationBuilder().build(
        mb_event,
        mb_market,
        observed_at=OBSERVED,
        quote_age_ms=120,
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event,
        pm_market,
        pm_books,
        observed_at=OBSERVED,
        quote_age_ms=180,
    )

    mb_yes = matchbook.book_for(CanonicalOutcome.YES)
    assert mb_yes is not None
    assert mb_yes.best_back is not None
    assert mb_yes.best_back.decimal_odds == Decimal("2.20")
    assert mb_yes.total_back_depth == Decimal("120")
    assert mb_yes.best_lay is not None
    assert mb_yes.best_lay.decimal_odds == Decimal("2.22")

    pm_no = polymarket.book_for(CanonicalOutcome.NO)
    assert pm_no is not None
    assert pm_no.best_back is not None
    assert pm_no.best_back.decimal_odds == Decimal("1") / Decimal("0.43")
    assert pm_no.best_back.available_stake == Decimal("68.80")
    assert pm_no.best_lay is not None
    assert pm_no.best_lay.decimal_odds == Decimal("1") / Decimal("0.41")


def test_matched_scan_records_shared_history_and_finds_depth_aware_paper_arb() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event,
        mb_market,
        observed_at=OBSERVED,
        source_latency_ms=80,
        quote_age_ms=120,
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event,
        pm_market,
        pm_books,
        observed_at=OBSERVED,
        source_latency_ms=110,
        quote_age_ms=180,
    )

    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx")
            ],
            capital_limit_gbp=Decimal("100"),
            maximum_execution_risk=100,
        )

        assert decision.market_match.matched is True
        assert decision.canonical_event_id is not None
        assert decision.canonical_market_id is not None
        assert decision.snapshots_recorded == 4
        assert decision.depth_scan is not None
        assert decision.depth_scan.solution.is_arbitrage is True
        assert decision.depth_scan.solution.guaranteed_profit > 0
        assert decision.depth_scan.combinations_evaluated > 0
        assert decision.execution_risk is not None
        assert decision.eligible_for_paper_simulation is True
        assert decision.rejection_reasons == []

        history = intelligence.market_history(canonical_market_id=decision.canonical_market_id)
        assert len(history) == 4
        assert {snapshot.venue for snapshot in history} == {
            VenueName.MATCHBOOK,
            VenueName.POLYMARKET,
        }
        assert {snapshot.canonical_event_id for snapshot in history} == {
            decision.canonical_event_id
        }
        assert {snapshot.metadata["native_currency"] for snapshot in history} == {"GBP", "USD"}
    finally:
        repository.close()


def test_cross_currency_scan_refuses_to_combine_depth_without_fx_snapshot() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(mb_event, mb_market, observed_at=OBSERVED)
    polymarket = PolymarketObservationBuilder().build(
        pm_event,
        pm_market,
        pm_books,
        observed_at=OBSERVED,
    )

    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
        )
        assert decision.depth_scan is None
        assert decision.eligible_for_paper_simulation is False
        assert "missing_fx_rate:USD" in decision.rejection_reasons
        assert decision.snapshots_recorded == 4
    finally:
        repository.close()


def test_missing_fee_assumption_does_not_invent_zero_cost_margin() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(mb_event, mb_market, observed_at=OBSERVED)
    polymarket = PolymarketObservationBuilder().build(
        pm_event,
        pm_market,
        pm_books,
        observed_at=OBSERVED,
    )

    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
        )
        assert decision.depth_scan is None
        assert decision.execution_risk is None
        assert decision.eligible_for_paper_simulation is False
        assert "missing_venue_cost:matchbook" in decision.rejection_reasons
        assert any(
            reason.startswith("unknown_required_venue_cost:polymarket") or reason == "unknown_costs"
            for reason in decision.rejection_reasons
        )
        pm_costs = [item for item in decision.venue_costs if item.venue is VenueName.POLYMARKET]
        assert pm_costs
        assert not pm_costs[0].is_economically_known()
    finally:
        repository.close()


def test_fee_snapshot_requires_explicit_rate_and_zero_basis() -> None:
    with pytest.raises(ValidationError):
        FeeSnapshot(venue=VenueName.MATCHBOOK)
    with pytest.raises(ValidationError, match="zero_rate_basis"):
        FeeSnapshot(venue=VenueName.MATCHBOOK, profit_haircut_rate=Decimal("0"))
    verified = FeeSnapshot(
        venue=VenueName.MATCHBOOK,
        profit_haircut_rate=Decimal("0"),
        zero_rate_basis="verified_zero",
    )
    assumed = FeeSnapshot(
        venue=VenueName.POLYMARKET,
        profit_haircut_rate=Decimal("0"),
        zero_rate_basis="assumed_zero",
    )
    assert verified.zero_rate_basis == "verified_zero"
    assert assumed.zero_rate_basis == "assumed_zero"
    with pytest.raises(ValidationError, match="timezone-aware"):
        FeeSnapshot(
            venue=VenueName.MATCHBOOK,
            profit_haircut_rate=Decimal("0.02"),
            captured_at=datetime(2026, 9, 12, 15, 0),
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        FxRateSnapshot(
            currency="USD",
            gbp_per_unit=Decimal("0.75"),
            captured_at=datetime(2026, 9, 12, 15, 0),
        )


def test_future_cost_snapshots_fail_closed_before_depth_scan() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    future = datetime.now(UTC) + timedelta(hours=1)

    try:
        fee_future = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs(captured_at=future),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
        )
        assert fee_future.depth_scan is None
        assert fee_future.execution_risk is None
        assert "future_fee_snapshot" in fee_future.rejection_reasons

        fx_future = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[
                FxRateSnapshot(
                    currency="USD",
                    gbp_per_unit=Decimal("0.75"),
                    captured_at=future,
                )
            ],
            maximum_execution_risk=100,
        )
        assert fx_future.depth_scan is None
        assert "future_fx_snapshot" in fx_future.rejection_reasons
    finally:
        repository.close()


def test_execution_risk_inputs_still_require_at_least_two_legs() -> None:
    with pytest.raises(ValidationError, match="leg_count"):
        ExecutionRiskInputs(
            spread_bps=0,
            size_to_depth_ratio=0,
            quote_age_ms=0,
            recent_volatility_bps=0,
            leg_count=0,
            minutes_to_kickoff=90,
            assumed_latency_ms=500,
        )
    with pytest.raises(ValidationError, match="leg_count"):
        ExecutionRiskInputs(
            spread_bps=0,
            size_to_depth_ratio=0,
            quote_age_ms=0,
            recent_volatility_bps=0,
            leg_count=1,
            minutes_to_kickoff=90,
            assumed_latency_ms=500,
        )


def test_complete_below_threshold_candidate_scores_risk_from_stakes() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )

    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            minimum_net_edge=Decimal("0.50"),
            maximum_execution_risk=0,
        )
        assert decision.depth_scan is not None
        assert decision.depth_scan.solution.is_arbitrage is True
        assert decision.depth_scan.solution.stakes
        assert len(decision.depth_scan.selected_quotes) >= 2
        assert decision.execution_risk is not None
        assert decision.eligible_for_paper_simulation is False
        assert "net_edge_below_threshold" in decision.rejection_reasons
        assert "execution_risk_above_threshold" in decision.rejection_reasons
        assert "missing_risk_evidence" not in decision.rejection_reasons
    finally:
        repository.close()


def test_negative_margin_without_stakes_rejects_missing_risk_evidence() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    pm_books = {
        **pm_books,
        "yes-token": {
            "asset_id": "yes-token",
            "bids": [{"price": "0.01", "size": "10"}],
            "asks": [{"price": "0.99", "size": "10"}],
        },
        "no-token": {
            "asset_id": "no-token",
            "bids": [{"price": "0.01", "size": "10"}],
            "asks": [{"price": "0.99", "size": "10"}],
        },
    }
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )

    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=0,
        )
        assert decision.depth_scan is not None
        assert decision.depth_scan.solution.is_arbitrage is False
        assert decision.depth_scan.solution.stakes == []
        assert len(decision.depth_scan.selected_quotes) >= 2
        assert decision.execution_risk is None
        assert decision.eligible_for_paper_simulation is False
        assert "no_arbitrage" in decision.rejection_reasons or "no_positive_edge" in decision.rejection_reasons
        assert "missing_risk_evidence" in decision.rejection_reasons
        assert "execution_risk_above_threshold" not in decision.rejection_reasons
    finally:
        repository.close()


def test_incomplete_settlement_fingerprints_are_not_equivalent() -> None:
    incomplete = SettlementFingerprint()
    other = SettlementFingerprint()
    assert incomplete.deterministic_key() == other.deterministic_key()
    assert incomplete.is_economically_complete() is False

    def event(venue: VenueName) -> CanonicalEvent:
        return CanonicalEvent(
            competition="Premier League",
            home_team="Newcastle United",
            away_team="Chelsea",
            kickoff_utc=KICKOFF,
            source_venue=venue,
            source_event_id=venue.value,
        )

    def market(venue: VenueName) -> CanonicalMarket:
        return CanonicalMarket(
            event=event(venue),
            source_venue=venue,
            source_market_id=f"{venue.value}-btts",
            family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            period=FootballPeriod.FULL_TIME,
            settlement=SettlementFingerprint(),
            runners=[
                CanonicalRunner(source_runner_id="y", outcome=CanonicalOutcome.YES, label="Yes"),
                CanonicalRunner(source_runner_id="n", outcome=CanonicalOutcome.NO, label="No"),
            ],
        )

    result = MarketMatcher().match(market(VenueName.MATCHBOOK), market(VenueName.POLYMARKET))
    assert result.matched is False
    assert "incomplete_settlement" in result.reasons


def test_legacy_fee_snapshot_alone_cannot_produce_a_strike() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            fee_snapshots=[
                FeeSnapshot(venue=VenueName.MATCHBOOK, profit_haircut_rate=Decimal("0.02")),
                FeeSnapshot(
                    venue=VenueName.POLYMARKET,
                    profit_haircut_rate=Decimal("0"),
                    zero_rate_basis="assumed_zero",
                ),
            ],
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
        )
        assert decision.depth_scan is None
        assert decision.eligible_for_paper_simulation is False
        assert "legacy_fee_snapshot_not_cost_truth" in decision.rejection_reasons
        assert "missing_venue_cost:matchbook" in decision.rejection_reasons
    finally:
        repository.close()


def test_unsupported_fee_scope_rejects_without_generic_haircut() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    costs = matchbook_polymarket_costs()
    costs[0] = costs[0].model_copy(update={"fee_scope": FeeScope.MARKET_NET_PNL})
    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=costs,
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
        )
        assert decision.eligible_for_paper_simulation is False
        assert "unsupported_fee_scope" in decision.rejection_reasons
        assert decision.depth_scan is None
        assert decision.eligible_for_paper_simulation is False
    finally:
        repository.close()


def test_explicit_supported_costs_change_guaranteed_payoff() -> None:
    from sports_hedge.config import Settings

    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    settings = Settings(max_slippage_bps=0, fx_spread_bps=0)
    service = PaperScanService(intelligence, settings=settings)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    fx = [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), spread_bps=Decimal("0"))]
    try:
        cheap = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs("0", "0"),
            fx_snapshots=fx,
            maximum_execution_risk=100,
        )
        dear = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs("0.05", "0"),
            fx_snapshots=fx,
            maximum_execution_risk=100,
        )
        assert cheap.depth_scan is not None and dear.depth_scan is not None
        assert cheap.depth_scan.solution.guaranteed_profit > dear.depth_scan.solution.guaranteed_profit
    finally:
        repository.close()


def test_configured_fx_spread_is_applied_and_labelled() -> None:
    from sports_hedge.config import Settings

    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(
        intelligence,
        settings=Settings(max_slippage_bps=0, fx_spread_bps=100),
    )
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
        )
        assert any(label.startswith("configured_fx_spread_bps:100") for label in decision.cost_assumption_labels)
        assert decision.depth_scan is not None
    finally:
        repository.close()
