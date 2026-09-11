from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.paper.models import FxRateSnapshot


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
        "startDate": KICKOFF.isoformat(),
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
            fee_snapshots=[
                FeeSnapshot(venue=VenueName.MATCHBOOK, profit_haircut_rate=Decimal("0.02")),
                FeeSnapshot(venue=VenueName.POLYMARKET, profit_haircut_rate=Decimal("0")),
            ],
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
            fee_snapshots=[
                FeeSnapshot(venue=VenueName.MATCHBOOK),
                FeeSnapshot(venue=VenueName.POLYMARKET),
            ],
        )
        assert decision.depth_scan is None
        assert decision.eligible_for_paper_simulation is False
        assert "missing_fx_rate:USD" in decision.rejection_reasons
        assert decision.snapshots_recorded == 4
    finally:
        repository.close()


def test_missing_fee_assumption_allows_diagnostic_scan_but_not_paper_eligibility() -> None:
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
        assert decision.depth_scan is not None
        assert decision.depth_scan.solution.is_arbitrage is True
        assert decision.eligible_for_paper_simulation is False
        assert "missing_fee_snapshot:matchbook" in decision.rejection_reasons
        assert "missing_fee_snapshot:polymarket" in decision.rejection_reasons
    finally:
        repository.close()
